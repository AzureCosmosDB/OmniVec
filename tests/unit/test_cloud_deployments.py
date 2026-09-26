import asyncio
import copy
import importlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from azure.cosmos.exceptions import CosmosAccessConditionFailedError
from fastapi import HTTPException

SUB = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
RG = f"/subscriptions/{SUB}/resourceGroups/deployment-test"
COSMOS = RG + "/providers/Microsoft.DocumentDB/databaseAccounts/testcosmos"
OPENAI = RG + "/providers/Microsoft.CognitiveServices/accounts/testopenai"


class MemoryStore:
    def __init__(self):
        self.docs = {}

    def get(self, key, partition):
        return copy.deepcopy(self.docs.get(key))

    def create(self, doc):
        assert doc["id"] not in self.docs
        saved = copy.deepcopy({**doc, "_etag": "1"})
        self.docs[doc["id"]] = saved
        return copy.deepcopy(saved)

    def replace_with_etag(self, doc, etag):
        if self.docs[doc["id"]]["_etag"] != etag:
            raise CosmosAccessConditionFailedError()
        self.docs[doc["id"]] = copy.deepcopy({**doc, "_etag": str(int(etag) + 1)})
        return self.get(doc["id"], doc["doc_type"])

    def query(self, query, parameters=None, partition_key=None):
        docs = [copy.deepcopy(d) for d in self.docs.values() if d["doc_type"] == partition_key]
        if "c.status IN" in query:
            docs = [d for d in docs if d["status"] in ("queued", "running")]
        return docs


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "api"))
    module = importlib.import_module("cloud_deployments")
    package = tmp_path / "mcp.zip"
    package.write_bytes(b"test-package")
    monkeypatch.setenv("OMNIVEC_MCP_PACKAGE", str(package))
    monkeypatch.setenv("AZURE_PRINCIPAL_ID", PRINCIPAL)
    monkeypatch.setenv("OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED", "true")
    monkeypatch.setenv("OMNIVEC_DEPLOYMENT_SCOPES", RG)
    store = MemoryStore()
    store.create({"id": "dst-1", "doc_type": "destination", "type": "cosmosdb-vector", "config": {
        "endpoint": "https://testcosmos.documents.azure.com", "database": "vectors", "container": "chunks", "key": "must-not-persist",
    }})
    store.create({"id": "mdl-1", "doc_type": "docgrok_model", "type": "azure-openai",
                  "endpoint": "https://testopenai.openai.azure.com", "deployment": "embedding",
                  "embedding_dim": 1536, "api_key": "must-not-persist"})
    body = module.DeploymentPlanRequest(kind="mcp", resource_group_id=RG, location="eastus2",
                                        destination_id="dst-1", embedding_model_id="mdl-1",
                                        cosmos_account_id=COSMOS, embedding_account_id=OPENAI)
    return module, store, body


def test_plan_is_safe_and_resource_specific(setup):
    module, store, body = setup
    doc = module.build_plan(body, store)
    assert doc["status"] == "awaiting_approval"
    assert not doc["attempts"]
    assert "must-not-persist" not in json.dumps(doc)
    assert all(r.startswith(RG + "/providers/") for r in doc["plan"]["resources"])
    assert doc["plan"]["permissions"][0]["scope"] == COSMOS + "/dbs/vectors/colls/chunks"
    for permission in doc["plan"]["deployer_permissions"]:
        assert PRINCIPAL in permission["commands"]["powershell"]
        assert permission["scope"] in permission["commands"]["bash"]
    assert len(store.docs) == 2  # Plan construction never writes Azure or metadata.


@pytest.mark.parametrize("changes", [
    {"resource_group_id": RG + "/../other"},
    {"cosmos_account_id": COSMOS + "?api-version=bad"},
    {"embedding_account_id": OPENAI.replace("testopenai", "different")},
    {"fields": "id,TOP 10"},
    {"fields": "id,../secret"},
])
def test_invalid_plan_rejected(setup, changes):
    module, store, body = setup
    with pytest.raises(ValueError):
        module.build_plan(body.model_copy(update=changes), store)


def test_dimension_and_registry_references_are_required(setup):
    module, store, body = setup
    store.docs["mdl-1"]["embedding_dim"] = 0
    with pytest.raises(ValueError, match="dimensions"):
        module.build_plan(body, store)
    store.docs["mdl-1"]["model_category"] = "chat"
    with pytest.raises(ValueError, match="embedding model"):
        module.build_plan(body, store)


@pytest.mark.parametrize("scopes", ["", "/", "/subscriptions/" + SUB, RG + "-different", RG + "/../other"])
def test_scope_is_fail_closed(setup, monkeypatch, scopes):
    module, store, body = setup
    doc = module.build_plan(body, store)
    monkeypatch.setenv("OMNIVEC_DEPLOYMENT_SCOPES", scopes)
    with pytest.raises(HTTPException):
        module.require_scopes(doc["plan"])


def test_references_and_package_changes_require_new_approval(setup):
    module, store, body = setup
    doc = module.build_plan(body, store)
    module.check_references(doc, store)
    module.package_path().write_bytes(b"different-build")
    with pytest.raises(ValueError, match="packaged server"):
        module.check_references(doc, store)
    store.docs["dst-1"]["_etag"] = "2"
    with pytest.raises(ValueError, match="destination or embedding"):
        module.check_references(doc, store)


@pytest.mark.asyncio
async def test_api_approval_is_admin_only_explicit_and_idempotent(api_app, setup, monkeypatch):
    module, store, body = setup
    monkeypatch.setattr(sys.modules["api"], "get_store", lambda: store)
    # Route installation captures the original getter, so patch its module singleton.
    monkeypatch.setattr(sys.modules["store"], "_store", store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        assert (await client.post("/api/cloud-deployments/plan", json=body.model_dump())).status_code == 401
        headers = {"Authorization": "Bearer test-admin-token"}
        plan = await client.post("/api/cloud-deployments/plan", json=body.model_dump(), headers=headers)
        assert plan.status_code == 200, plan.text
        doc = plan.json()
        assert doc["status"] == "awaiting_approval"
        exact = await client.get(
            "/api/cloud-deployments/" + doc["id"], headers=headers)
        assert exact.status_code == 200
        assert exact.json()["id"] == doc["id"]
        assert (await client.get(
            "/api/cloud-deployments/deploy-missing", headers=headers
        )).status_code == 404
        url = "/api/cloud-deployments/" + doc["id"] + "/approve"
        approval = {"plan_hash": doc["plan_hash"], "approve_cost_and_permissions": True}
        assert (await client.post(url, json={**approval, "approve_cost_and_permissions": False}, headers=headers)).status_code == 422
        assert (await client.post(url, json={**approval, "plan_hash": "changed"}, headers=headers)).status_code == 409
        monkeypatch.setenv("OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED", "false")
        assert (await client.post(url, json=approval, headers=headers)).status_code == 409
        monkeypatch.setenv("OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED", "true")
        first = await client.post(url, json=approval, headers=headers)
        second = await client.post(url, json=approval, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["status"] == "queued"
        assert store.docs[doc["id"]]["attempts"] == 0
        assert len(store.docs[doc["id"]]["approval_history"]) == 1
        store.docs[doc["id"]].update(status="failed", attempts=3)
        assert (await client.post(url, json=approval, headers=headers)).status_code == 409
        store.docs[doc["id"]].update(status="awaiting_approval", attempts=0, created_at=time.time() - 1801)
        assert (await client.post(url, json=approval, headers=headers)).status_code == 409
        monkeypatch.setattr(sys.modules["api"], "_validate_token", lambda token: {"role": "viewer"})
        assert (await client.get("/api/cloud-deployments", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_internal_host_does_not_authorize_deployment(api_app, setup):
    _, _, body = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://omnivec-api") as client:
        response = await client.post("/api/cloud-deployments/plan", json=body.model_dump())
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_worker_interruption_is_not_replayed(setup, monkeypatch):
    module, store, body = setup
    doc = module.build_plan(body, store)
    doc.update(status="running", lease_until=time.time() - 1, attempts=1)
    store.create(doc)
    async def stop(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(module.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await module.deployment_worker(lambda: store)
    assert store.docs[doc["id"]]["status"] == "interrupted"
    assert store.docs[doc["id"]]["attempts"] == 1


@pytest.mark.asyncio
async def test_disabled_worker_does_not_poll_metadata(setup, monkeypatch):
    module, _, _ = setup
    monkeypatch.setenv("OMNIVEC_CLOUD_DEPLOYMENTS_ENABLED", "false")
    async def stop(_):
        raise asyncio.CancelledError()
    def forbidden_store():
        pytest.fail("A disabled deployment worker must not poll metadata.")
    monkeypatch.setattr(module.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await module.deployment_worker(forbidden_store)


class Credential:
    def __init__(self, **kwargs):
        pass

    def get_token(self, *args):
        return SimpleNamespace(token="test-arm-token")

    def close(self):
        pass


@pytest.fixture
def azure(setup, monkeypatch):
    module, store, body = setup
    engine = importlib.import_module("cloud_deployment_azure")
    monkeypatch.setattr(engine, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(engine.time, "sleep", lambda seconds: None)
    doc = module.build_plan(body, store)
    doc.update(status="running", lease_until=time.time() + 1800)
    requests = []
    resources = {}
    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.url.host.endswith(".azurewebsites.net"):
            if path == "/api/publish":
                return httpx.Response(202, json={})
            if path == "/api/deployments/latest":
                return httpx.Response(200, json={"status": 4})
            if "x-functions-key" not in request.headers:
                return httpx.Response(401)
            message = json.loads(request.content)
            method = message["method"]
            if method == "initialize":
                result = {"serverInfo": {"deployment_id": doc["id"]}}
            elif method == "tools/list":
                result = {"tools": [{"name": "list_allowed_containers"}, {"name": "vector_search"}]}
            else:
                result = {"content": [{"type": "text", "text": "{\"matches\": []}"}], "isError": False}
            return httpx.Response(200, json={"result": result})
        if request.method == "PUT":
            value = json.loads(request.content)
            if path == doc["plan"]["function_id"]:
                value["identity"]["principalId"] = PRINCIPAL
                value["properties"].update(defaultHostName="test.azurewebsites.net", enabledHostNames=["test.scm.azurewebsites.net"])
            for index in value.get("properties", {}).get("resource", {}).get("indexingPolicy", {}).get("vectorIndexes", []):
                index["quantizationByteSize"] = 96
            resources[path] = value
            return httpx.Response(200, json=value)
        if path in resources:
            return httpx.Response(200, json=resources[path])
        if path == RG:
            return httpx.Response(200, json={})
        if path == COSMOS + "/sqlDatabases/vectors/containers/chunks":
            return httpx.Response(200, json={"properties": {"resource": {
                "vectorEmbeddingPolicy": {"vectorEmbeddings": [{"path": "/embedding", "dimensions": 1536}]},
                "indexingPolicy": {"vectorIndexes": [{"path": "/embedding"}]},
            }}})
        if "/deployments/embedding" in path:
            return httpx.Response(200, json={"properties": {"model": {"name": "text-embedding-3-small"}}})
        if path.endswith("/listkeys"):
            return httpx.Response(200, json={"functionKeys": {"default": "never-persist-function-key"}})
        return httpx.Response(404)
    def progress(stage, **changes):
        doc.update(stage=stage, **changes)
    deployment = engine.AzureDeployment(doc, progress)
    deployment.client.close()
    deployment.client = httpx.Client(transport=httpx.MockTransport(handler))
    return engine, deployment, doc, requests, resources


def test_mcp_deployment_uses_bundled_code_scoped_roles_and_no_inference(azure):
    engine, deployment, doc, requests, resources = azure
    with deployment:
        result = deployment.run()
    assert result["server_url"] == "https://test.azurewebsites.net/api/mcp"
    assert "not yet verified" in result["verification"]
    assert "never-persist-function-key" not in json.dumps(doc) + json.dumps(result)
    publish = next(r for r in requests if r.url.path == "/api/publish")
    assert publish.content == b"test-package"
    assert publish.url.params["RemoteBuild"] == "false"
    assert not any("embeddings" in str(r.url) for r in requests)
    assert resources[doc["plan"]["storage_id"]]["properties"]["allowSharedKeyAccess"] is False
    assignments = [v for k, v in resources.items() if "/sqlRoleAssignments/" in k]
    assert assignments[0]["properties"]["scope"] == COSMOS + "/dbs/vectors/colls/chunks"


def test_existing_unowned_resource_is_never_overwritten(azure):
    engine, deployment, doc, requests, resources = azure
    resources[doc["plan"]["storage_id"]] = {"tags": {}}
    with deployment, pytest.raises(engine.DeploymentFailure, match="not owned"):
        deployment.run()
    assert not any(r.method == "PUT" for r in requests)


def test_azure_errors_never_leak_response_body(azure):
    engine, deployment, doc, requests, resources = azure
    deployment.client.close()
    deployment.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": "secret-key-in-response"})))
    with deployment, pytest.raises(engine.DeploymentFailure) as error:
        deployment.run()
    assert error.value.code == "access_denied"
    assert error.value.resource_id == RG
    assert "secret-key-in-response" not in str(error.value)


def test_vector_mismatch_stops_before_provisioning(azure):
    engine, deployment, doc, requests, resources = azure
    resources[COSMOS + "/sqlDatabases/vectors/containers/chunks"] = {
        "properties": {"resource": {"vectorEmbeddingPolicy": {"vectorEmbeddings": []}}}}
    with deployment, pytest.raises(engine.DeploymentFailure) as error:
        deployment.run()
    assert error.value.code == "vector_mismatch"
    assert not any(r.method == "PUT" for r in requests)


def test_foundry_permissions_use_stable_account_scoped_role(setup, azure):
    module, store, _ = setup
    _, _, mcp, _, _ = azure
    mcp.update(status="succeeded", result={"server_url": "https://test.azurewebsites.net/api/mcp"})
    store.create(mcp)
    foundry = module.build_plan(module.DeploymentPlanRequest(
        kind="foundry", resource_group_id=RG, location="eastus2",
        mcp_deployment_id=mcp["id"], project_resource_id=OPENAI + "/projects/project",
        chat_deployment="chat",
    ), store)
    permission = next(item for item in foundry["plan"]["deployer_permissions"] if item["role"] == "Foundry User")
    assert permission["scope"] == OPENAI
    assert permission["role_definition_id"] == module.FOUNDRY_USER_ROLE_ID
    assert f"--role {module.FOUNDRY_USER_ROLE_ID}" in permission["commands"]["bash"]


def test_wait_tolerates_transient_resource_not_found(azure):
    _, deployment, doc, _, _ = azure
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(404)
        return httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}})
    deployment.client.close()
    deployment.client = httpx.Client(transport=httpx.MockTransport(handler))
    with deployment:
        result = deployment.wait(doc["plan"]["storage_id"], "2023-05-01")
    assert result["properties"]["provisioningState"] == "Succeeded"
    assert calls == 2


def test_worker_persists_failure_without_secrets(setup, monkeypatch):
    module, store, body = setup
    engine = importlib.import_module("cloud_deployment_azure")
    doc = module.build_plan(body, store)
    doc.update(status="running", lease_until=time.time() + 1800, attempts=1)
    doc = store.create(doc)
    class Fail:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self):
            raise RuntimeError("token=never-log-this")
    monkeypatch.setattr(engine, "AzureDeployment", Fail)
    module.execute_job(store, doc)
    saved = store.docs[doc["id"]]
    assert saved["status"] == "failed"
    assert saved["plan"]["resources"]
    assert "never-log-this" not in json.dumps(saved)


@pytest.mark.parametrize("mode", ["create", "reconcile", "ambiguous", "conflict"])
def test_foundry_creation_is_secret_safe_and_never_blindly_replayed(setup, azure, monkeypatch, mode):
    module, store, body = setup
    engine, deployment, mcp, requests, resources = azure
    mcp.update(status="succeeded", result={"server_url": "https://test.azurewebsites.net/api/mcp"})
    store.create(mcp)
    foundry = module.build_plan(module.DeploymentPlanRequest(
        kind="foundry", resource_group_id=RG, location="eastus2",
        mcp_deployment_id=mcp["id"], project_resource_id=OPENAI + "/projects/project",
        chat_deployment="chat",
    ), store)
    if mode == "ambiguous":
        foundry["agent_create_started"] = True
    deployment.doc, deployment.plan = foundry, foundry["plan"]
    def progress(stage, **changes):
        foundry.update(stage=stage, **changes)
    deployment.progress = progress
    connection_requests = []
    def handler(request):
        if request.method == "PUT":
            connection_requests.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.url.path.endswith("/listkeys"):
            return httpx.Response(200, json={"functionKeys": {"default": "secret-function-key"}})
        if "/connections/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"properties": {
            "endpoints": {"AI Foundry API": "https://testopenai.services.ai.azure.com/api/projects/project"},
            "model": {"name": "gpt-4o"},
        }})
    deployment.client.close()
    deployment.client = httpx.Client(transport=httpx.MockTransport(handler))
    created = []
    version = SimpleNamespace(name=foundry["plan"]["agent_name"], version="1", metadata={"omnivec-deployment": foundry["id"]})
    class Agents:
        def list_versions(self, *args, **kwargs):
            if mode == "conflict":
                return iter([SimpleNamespace(metadata={"omnivec-deployment": "other"})])
            return iter([version] if mode == "reconcile" else [])

        def create_version(self, **kwargs):
            created.append(kwargs)
            return version
    class Project:
        agents = Agents()

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass
    import azure.ai.projects
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", Project)
    with deployment:
        if mode in ("ambiguous", "conflict"):
            with pytest.raises(engine.DeploymentFailure) as error:
                deployment.run()
            assert error.value.code == ("agent_creation_uncertain" if mode == "ambiguous" else "agent_conflict")
        else:
            result = deployment.run()
            assert result["agent_version"] == "1"
            assert "end-to-end retrieval remains unverified" in result["verification"]
    assert len(created) == (1 if mode == "create" else 0)
    assert connection_requests[0]["properties"]["credentials"]["keys"] == {"x-functions-key": "secret-function-key"}
    assert "secret-function-key" not in json.dumps(foundry)
    if created:
        definition = created[0]["definition"].as_dict()
        assert definition["tools"][0]["allowed_tools"] == ["list_allowed_containers", "vector_search"]
        assert definition["tools"][0]["project_connection_id"] == foundry["plan"]["connection_id"]


def test_isolated_container_plan_and_actual_vector_policy(setup, azure):
    module, store, body = setup
    engine, deployment, _, requests, resources = azure
    doc = module.build_plan(module.DeploymentPlanRequest(
        kind="cosmos_container", resource_group_id=RG, location="eastus2",
        cosmos_account_id=COSMOS, database="vectors", container="fresh-demo",
        embedding_model_id="mdl-1", partition_key_path="/document_id",
    ), store)
    resources[COSMOS] = {"location": "westus3"}
    resources[COSMOS + "/sqlDatabases/vectors"] = {}
    deployment.doc, deployment.plan = doc, doc["plan"]
    deployment.progress = lambda stage, **kwargs: doc.update(stage=stage, **kwargs)
    with deployment:
        result = deployment.run()
    actual = resources[result["container_id"]]
    assert actual["location"] == "westus3"  # Existing account location, not arbitrary form location.
    assert actual["properties"]["options"] == {}  # No unsupported fixed throughput on serverless.
    assert actual["properties"]["resource"]["partitionKey"]["paths"] == ["/document_id"]
    assert actual["properties"]["resource"]["vectorEmbeddingPolicy"]["vectorEmbeddings"][0]["dimensions"] == 1536
    assert actual["properties"]["resource"]["indexingPolicy"]["vectorIndexes"][0]["quantizationByteSize"] == 96
    assert actual["properties"]["resource"]["indexingPolicy"]["vectorIndexes"][0]["path"] == "/embedding"
    assert len([r for r in requests if r.method == "PUT"]) == 1
    assert doc["plan"]["authorization_scopes"] == [COSMOS]
    assert doc["plan"]["partition_key_path"] == "/document_id"


@pytest.mark.parametrize("with_references", [True, False])
def test_foundry_test_requires_retrieval_evidence_and_never_replays(setup, azure, monkeypatch, with_references):
    module, store, body = setup
    engine, deployment, mcp, _, _ = azure
    mcp.update(status="succeeded", result={"server_url": "https://test.azurewebsites.net/api/mcp"})
    store.create(mcp)
    parent = module.build_plan(module.DeploymentPlanRequest(
        kind="foundry", resource_group_id=RG, location="eastus2",
        mcp_deployment_id=mcp["id"], project_resource_id=OPENAI + "/projects/project",
        chat_deployment="chat",
    ), store)
    parent.update(status="succeeded", result={"agent_name": parent["plan"]["agent_name"], "agent_version": "1"})
    store.create(parent)
    doc = module.build_plan(module.DeploymentPlanRequest(
        kind="verification", resource_group_id=RG, location="eastus2",
        foundry_deployment_id=parent["id"], question="What is the daily meal limit?",
    ), store)
    deployment.doc, deployment.plan = doc, doc["plan"]
    deployment.progress = lambda stage, **kwargs: doc.update(stage=stage, **kwargs)
    monkeypatch.setattr(deployment, "project_endpoint", lambda rid: "https://testopenai.services.ai.azure.com/api/projects/project")
    calls = []
    output = [SimpleNamespace(type="mcp_call", name="vector_search", server_label="omnivec-cosmos", error=None,
                              output=json.dumps({"content": [{"type": "text", "text": json.dumps({
                                  "matches": [{"id": "fresh-1", "title": "Northstar Demo Travel Policy", "source_ref": "01-travel-policy.txt"}],
                              })}]}))] if with_references else []
    class OpenAI:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0, "timeout": 120}
            self.responses = self
            return self

        def create(self, **kwargs):
            assert doc["inference_started"] is True
            calls.append(kwargs)
            return SimpleNamespace(id="response-1", status="completed", output_text="47 USD [01-travel-policy.txt]", output=output)
    class Project:
        def __init__(self, **kwargs):
            self.agents = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_version(self, name, version):
            assert version == "1"
            return SimpleNamespace(metadata={"omnivec-deployment": parent["id"]})

        def get_openai_client(self):
            return OpenAI()
    monkeypatch.setattr(importlib.import_module("azure.ai.projects"), "AIProjectClient", Project)
    with deployment:
        if with_references:
            result = deployment.run()
            assert result["source_references"][0]["source_ref"] == "01-travel-policy.txt"
            assert "47 USD" in result["answer"]
        else:
            with pytest.raises(engine.DeploymentFailure) as error:
                deployment.run()
            assert error.value.code == "retrieval_not_verified"
        with pytest.raises(engine.DeploymentFailure) as repeated:
            deployment.run()
        assert repeated.value.code == "inference_uncertain"
    assert len(calls) == 1
    assert calls[0]["store"] is False
    assert calls[0]["extra_body"]["agent_reference"]["version"] == "1"
