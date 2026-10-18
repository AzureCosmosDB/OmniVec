"""Permission-aware MCP regressions using local JWTs and HTTP/Cosmos doubles."""

import asyncio
import base64
import copy
import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa


MCP_PATH = Path(__file__).parents[2] / "mcp_servers" / "cosmos"
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
OTHER_TENANT = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(MCP_PATH))
    return SimpleNamespace(
        auth=importlib.import_module("mcp_auth"),
        retrieval=importlib.import_module("sharepoint_retrieval"),
    )


@pytest.fixture
def settings(modules, monkeypatch):
    for key, value in {
        "COSMOS_ALLOWED_CONTAINERS": "protected,public",
        "COSMOS_DEFAULT_CONTAINER": "protected",
        "COSMOS_DATABASE": "test",
        "COSMOS_SHAREPOINT_PROTECTED_CONTAINERS": "protected",
        "MCP_ENTRA_TENANT_ID": TENANT,
        "MCP_ENTRA_CLIENT_ID": CLIENT,
        "MCP_ENTRA_CLIENT_SECRET": "test-only-not-a-real-secret",
        "MCP_PUBLIC_URL": "https://mcp.example.test/api/mcp",
    }.items():
        monkeypatch.setenv(key, value)
    return modules.auth.load_security_settings(("protected", "public"))


@pytest.fixture
def principal(modules):
    return modules.auth.Caller(TENANT, USER, "test-user-assertion")


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def user_token(signing_key, **changes):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT, "tid": TENANT, "oid": USER, "ver": "2.0",
        "iat": now, "nbf": now - 1, "exp": now + 300, "scp": "access_as_user",
    }
    claims.update(changes)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize("changes", [
    {"aud": "https://graph.microsoft.com"}, {"tid": OTHER_TENANT},
    {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"},
    {"exp": 1}, {"scp": ""}, {"scp": None, "roles": ["Application.Read"]},
    {"oid": ""}, {"ver": "1.0"}, {"nbf": 9999999999},
])
def test_jwt_requires_own_audience_same_tenant_and_user_scope(
    modules, settings, signing_key, monkeypatch, changes,
):
    authenticator = modules.auth.EntraAuthenticator(settings)
    monkeypatch.setattr(authenticator._keys, "get_signing_key_from_jwt",
                        lambda _: SimpleNamespace(key=signing_key.public_key()))
    with pytest.raises(modules.auth.AuthenticationRequired):
        authenticator.authenticate("Bearer " + user_token(signing_key, **changes))
    caller = authenticator.authenticate("Bearer " + user_token(signing_key))
    assert caller.tenant_id == TENANT and caller.object_id == USER
    assert caller.access_token not in repr(caller)


def test_jwt_signature_and_missing_bearer_fail_without_function_key_fallback(
    modules, settings, signing_key, monkeypatch,
):
    authenticator = modules.auth.EntraAuthenticator(settings)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(authenticator._keys, "get_signing_key_from_jwt",
                        lambda _: SimpleNamespace(key=other_key.public_key()))
    for header in (None, "function-key", "Bearer " + user_token(signing_key)):
        with pytest.raises(modules.auth.AuthenticationRequired):
            authenticator.authenticate(header)


def test_obo_uses_callers_assertion_and_surfaces_consent_without_app_fallback(
    modules, settings, principal,
):
    authenticator = modules.auth.EntraAuthenticator(settings)
    calls = []
    claims = '{"access_token":{"acrs":{"essential":true,"value":"c1"}}}'

    def acquire(assertion, scopes):
        calls.append((assertion, scopes))
        return {"error": "interaction_required", "claims": claims}

    authenticator._application = SimpleNamespace(acquire_token_on_behalf_of=acquire)
    with pytest.raises(modules.auth.AuthenticationRequired) as failure:
        authenticator.graph_token(principal)
    assert calls == [(principal.access_token, ["https://graph.microsoft.com/.default"])]
    assert json.loads(base64.b64decode(failure.value.claims)) == json.loads(claims)
    assert "insufficient_claims" in settings.challenge(failure.value.claims)


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.body

    @property
    def content(self):
        raise AssertionError("Authorization must not download file bodies")

    def close(self):
        self.closed = True


class Graph:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["headers"]["Authorization"] == "Bearer delegated-test-token"
        self.calls.append(url)
        return self.respond(url)

    def close(self):
        pass


def provenance(modules, item="item-1", tenant=TENANT):
    return modules.retrieval.Provenance(tenant, "site", "drive", item, "v1")


def graph_metadata(item="item-1", etag="v1"):
    return {"id": item, "eTag": etag, "file": {}, "sharepointIds": {"tenantId": TENANT}}


def authorizer(modules, principal, graph):
    result = modules.retrieval.SharePointAuthorizer(principal, lambda: "delegated-test-token")
    result._session.close()
    result._session = graph
    return result


def test_live_read_checks_are_per_user_and_revocations_are_not_cached_across_requests(modules, principal):
    permitted = True

    def respond(url):
        if url.endswith("/content"):
            return Response(302 if permitted else 403, headers={"Location": "https://storage.example.test/private-token"})
        return Response(200, graph_metadata())

    graph = Graph(respond)
    first = authorizer(modules, principal, graph)
    assert first.allowed(provenance(modules))
    assert first.allowed(provenance(modules))
    assert len(graph.calls) == 3
    permitted = False
    second = authorizer(modules, principal, graph)
    assert not second.allowed(provenance(modules))
    other_user = modules.auth.Caller(TENANT, OTHER_TENANT, "another-user-assertion")
    assert not authorizer(modules, other_user, graph).allowed(provenance(modules))
    assert len(graph.calls) == 7


@pytest.mark.parametrize("failure", [
    "etag", "tenant", "malformed-tenant", "deleted", "content", "changed-during-read",
])
def test_metadata_visibility_is_not_sufficient_to_release_cached_text(modules, principal, failure):
    metadata_reads = 0

    def respond(url):
        nonlocal metadata_reads
        if url.endswith("/content"):
            return Response(403 if failure == "content" else 302, headers={"Location": "https://storage.example.test/file"})
        metadata_reads += 1
        body = graph_metadata()
        if failure == "etag" or (failure == "changed-during-read" and metadata_reads > 1):
            body["eTag"] = "v2"
        if failure == "tenant":
            body["sharepointIds"]["tenantId"] = OTHER_TENANT
        if failure == "malformed-tenant":
            body["sharepointIds"]["tenantId"] = None
        if failure == "deleted":
            body["deleted"] = {}
        return Response(200, body)

    assert not authorizer(modules, principal, Graph(respond)).allowed(provenance(modules))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_graph_failures_are_explicit_and_retries_are_bounded(modules, principal, monkeypatch, status):
    graph = Graph(lambda _: Response(status))
    monkeypatch.setattr(modules.retrieval.time, "sleep", lambda _: None)
    with pytest.raises(modules.auth.AuthorizationUnavailable):
        authorizer(modules, principal, graph).allowed(provenance(modules))
    assert len(graph.calls) == 3


def test_graph_timeout_and_claims_challenge_do_not_look_like_empty_results(modules, principal):
    def timeout(_):
        raise requests.Timeout("private upstream details")

    with pytest.raises(modules.auth.AuthorizationUnavailable, match="retry") as failure:
        authorizer(modules, principal, Graph(timeout)).allowed(provenance(modules))
    assert "private" not in str(failure.value)
    claims = base64.b64encode(b'{"access_token":{"acrs":{"essential":true}}}').decode()
    graph = Graph(lambda _: Response(401, headers={"WWW-Authenticate": f'Bearer claims="{claims}"'}))
    with pytest.raises(modules.auth.AuthenticationRequired) as failure:
        authorizer(modules, principal, graph).allowed(provenance(modules))
    assert failure.value.claims == claims


def test_tenant_mismatch_never_calls_graph_or_acquires_a_token(modules, principal):
    graph = Graph(lambda _: pytest.fail("Wrong-tenant Graph call"))
    assert not authorizer(modules, principal, graph).allowed(provenance(modules, tenant=OTHER_TENANT))
    assert graph.calls == []


def test_graph_claims_accept_urlsafe_unpadded_values_but_not_arbitrary_headers(modules, principal):
    claims = '{"access_token":{"value":"\u0fff"}}'
    encoded = base64.urlsafe_b64encode(claims.encode()).decode().rstrip("=")
    graph = Graph(lambda _: Response(403, headers={"WWW-Authenticate": f'bearer claims="{encoded}"'}))
    with pytest.raises(modules.auth.AuthenticationRequired) as failure:
        authorizer(modules, principal, graph).allowed(provenance(modules))
    assert json.loads(base64.b64decode(failure.value.claims)) == json.loads(claims)
    for value in ("not-base64", base64.b64encode(b'"not-an-object"').decode(), "x" * 12001):
        with pytest.raises(modules.auth.AuthorizationUnavailable):
            modules.auth.encode_claims(value, encoded=True)


def chunk(number=1, index=0):
    identity = f"sp-{number:064x}"
    return {
        "id": f"{identity}-1-chunk-{index}", "document": {"key": identity},
        "source_id": "source", "pipeline_id": "pipeline", "source_ref": f"file-{number}.txt",
        "content": f"private text {number}", "embedding": [0.1, 0.2],
        "_omnivec_sync": {"kind": "chunk", "identity": identity, "revision": 1},
        "_omnivec_provenance": {
            "schema_version": 1, "provider": "sharepoint", "resource_tenant_id": TENANT,
            "site_id": "site", "drive_id": "drive", "item_id": f"item-{number}", "source_etag": "v1",
        },
    }


class Cosmos:
    def __init__(self, documents):
        self.documents = {doc["id"]: copy.deepcopy(doc) for doc in documents}
        self.candidates = []
        self.reads = []
        self.query = None
        for document in documents:
            identity = document["_omnivec_sync"]["identity"]
            candidate = {name: copy.deepcopy(document[name]) for name in (
                "id", "source_id", "pipeline_id", "source_ref", "_omnivec_sync", "_omnivec_provenance"
            )}
            candidate.update(_partition_key=identity, distance=0.1)
            self.candidates.append(candidate)
            state = copy.deepcopy(candidate)
            state["id"] = identity + "-state"
            state["_omnivec_sync"].update(kind="state", completed=True)
            self.documents[state["id"]] = state

    def read(self):
        return {"partitionKey": {"paths": ["/document/key"]}}

    def query_items(self, **kwargs):
        self.query = kwargs
        assert "c.content" not in kwargs["query"] and "SELECT *" not in kwargs["query"]
        return iter(self.candidates)

    def read_item(self, item, partition_key):
        self.reads.append(item)
        assert item.startswith(partition_key)
        return copy.deepcopy(self.documents[item])


def read_authorizer(principal, allowed):
    return SimpleNamespace(caller=principal, remaining=lambda: 20, allowed=allowed)


@pytest.mark.parametrize("vector", [None, "embedding"])
def test_search_and_listing_only_fetch_authorized_current_text(modules, principal, vector):
    denied, allowed = chunk(1), chunk(2)
    client = Cosmos([denied, allowed])
    read = modules.retrieval.protected_documents(
        client, read_authorizer(principal, lambda p: p.item_id == "item-2"),
        "id,content,source_ref", ("id", "content", "source_ref"), 1,
        vector_field=vector, embedding=[0.1, 0.2] if vector else None,
    )
    assert len(read) == 1 and read[0]["content"] == allowed["content"]
    assert denied["id"] not in client.reads
    assert denied["content"] not in json.dumps(read)
    assert "_omnivec_provenance" not in read[0]
    assert {"name": "@tenant", "value": TENANT} in client.query["parameters"]
    assert "c[\"document\"][\"key\"] AS _partition_key" in client.query["query"]


@pytest.mark.parametrize("mutation", ["missing", "wrong-tenant", "incomplete", "obsolete", "partition", "boolean-revision"])
def test_legacy_tampered_and_uncommitted_chunks_cannot_reach_graph_or_results(modules, principal, mutation):
    document = chunk()
    client = Cosmos([document])
    candidate = client.candidates[0]
    state = client.documents[document["_omnivec_sync"]["identity"] + "-state"]
    if mutation == "missing":
        candidate.pop("_omnivec_provenance")
    elif mutation == "wrong-tenant":
        candidate["_omnivec_provenance"]["resource_tenant_id"] = OTHER_TENANT
    elif mutation == "incomplete":
        state["_omnivec_sync"]["completed"] = False
    elif mutation == "obsolete":
        state["_omnivec_sync"]["revision"] = 2
    elif mutation == "partition":
        candidate["_partition_key"] = "unrelated"
    else:
        candidate["_omnivec_sync"]["revision"] = True
    result = modules.retrieval.protected_documents(
        client, read_authorizer(principal, lambda _: pytest.fail("Invalid candidate authorized")),
        "id", ("id",), 1,
    )
    assert result == [] and document["id"] not in client.reads


def test_storage_changes_after_authorization_cannot_release_different_text(modules, principal):
    document = chunk()
    client = Cosmos([document])

    def allow(_):
        client.documents[document["id"]]["_omnivec_provenance"]["source_etag"] = "changed"
        return True

    result = modules.retrieval.protected_documents(
        client, read_authorizer(principal, allow), "content", ("content",), 1,
    )
    assert result == []


def test_new_revision_after_chunk_read_is_excluded(modules, principal, monkeypatch):
    document = chunk()
    client = Cosmos([document])
    read_item = client.read_item

    def racing_read(item, partition_key):
        result = read_item(item, partition_key)
        if item == document["id"]:
            client.documents[partition_key + "-state"]["_omnivec_sync"]["revision"] = 2
        return result

    monkeypatch.setattr(client, "read_item", racing_read)
    result = modules.retrieval.protected_documents(
        client, read_authorizer(principal, lambda _: True), "content", ("content",), 1,
    )
    assert result == []


def test_denials_do_not_cause_unbounded_overfetch(modules, principal):
    documents = [chunk(number) for number in range(1, 61)]
    client = Cosmos(documents)
    checked = []

    def allow(value):
        checked.append(value.item_id)
        return value.item_id == "item-51"

    result = modules.retrieval.protected_documents(
        client, read_authorizer(principal, allow), "content", ("content",), 1,
        vector_field="embedding", embedding=[0.1, 0.2],
    )
    assert result == [] and len(checked) == 50
    assert "SELECT TOP 50" in client.query["query"]
    assert all(document["id"] not in client.reads for document in documents)


def test_projection_cannot_omit_authorization_or_expose_internal_metadata(modules, principal):
    client = Cosmos([chunk()])
    checker = read_authorizer(principal, lambda _: False)
    assert modules.retrieval.protected_documents(client, checker, "id", ("id",), 1) == []
    for fields in ("_omnivec_provenance", "_omnivec_sync", "embedding", "unapproved"):
        with pytest.raises(ValueError, match="not permitted"):
            modules.retrieval.protected_documents(client, checker, fields, ("id",), 1, vector_field="embedding")


@pytest.fixture
def function_app(settings, modules, principal, monkeypatch):
    spec = importlib.util.spec_from_file_location("protected_cosmos_mcp", MCP_PATH / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    def authenticate(header):
        if header != "Bearer test-user-assertion":
            raise modules.auth.AuthenticationRequired()
        return principal

    monkeypatch.setattr(module, "_authenticator", lambda: SimpleNamespace(authenticate=authenticate))
    return module


def http_request(function_app, message, *, signed_in=True):
    return function_app.func.HttpRequest(
        "POST", "https://mcp.example.test/api/mcp",
        headers={"Authorization": "Bearer test-user-assertion"} if signed_in else {"x-functions-key": "test-key"},
        params={}, body=json.dumps(message).encode(),
    )


def tool_message(name="list_documents", arguments=None):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def test_function_key_and_native_extension_cannot_bypass_user_auth(function_app, modules, monkeypatch):
    monkeypatch.setattr(function_app, "_container", lambda _: pytest.fail("Unauthenticated database access"))
    response = function_app.mcp_http(http_request(function_app, tool_message(), signed_in=False))
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["WWW-Authenticate"]
    for function in function_app.app.get_functions():
        if function.get_function_name() in {"list_documents", "vector_search", "list_allowed_containers"}:
            kwargs = {"question": "find documents"} if function.get_function_name() == "vector_search" else {}
            with pytest.raises(modules.auth.AuthenticationRequired):
                asyncio.run(function.get_user_function()(json.dumps({"arguments": kwargs})))


def test_http_routes_current_authorized_rows_and_rejects_caller_argument(function_app, modules, principal, monkeypatch):
    client = Cosmos([chunk()])
    monkeypatch.setattr(function_app, "_container", lambda _: ("protected", client))
    monkeypatch.setattr(function_app, "SharePointAuthorizer",
                        lambda caller, _: SimpleNamespace(
                            caller=caller, remaining=lambda: 20, allowed=lambda _: True, close=lambda: None,
                        ))
    response = function_app.mcp_http(http_request(function_app, tool_message(arguments={"fields": "id,content"})))
    result = json.loads(response.get_body())["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["documents"][0]["content"] == "private text 1"
    response = function_app.mcp_http(http_request(
        function_app, tool_message(arguments={"_caller": {"tenant_id": OTHER_TENANT}}),
    ))
    assert json.loads(response.get_body())["result"]["isError"] is True


def test_graph_challenges_propagate_to_http_and_failures_remain_tool_errors(function_app, modules, monkeypatch):
    monkeypatch.setattr(function_app, "_container", lambda _: ("protected", object()))

    def fail(*_, **__):
        raise modules.auth.AuthenticationRequired()

    monkeypatch.setattr(function_app, "_read_protected", fail)
    response = function_app.mcp_http(http_request(function_app, tool_message()))
    assert response.status_code == 401

    def unavailable(*_, **__):
        raise modules.auth.AuthorizationUnavailable()

    monkeypatch.setattr(function_app, "_read_protected", unavailable)
    response = function_app.mcp_http(http_request(function_app, tool_message()))
    assert json.loads(response.get_body())["result"]["isError"] is True


def test_oauth_metadata_and_bad_configuration_never_enable_anonymous_reads(function_app, monkeypatch):
    request = http_request(function_app, {})
    response = function_app.oauth_protected_resource(request)
    body = json.loads(response.get_body())
    assert body["resource"] == "https://mcp.example.test/api/mcp"
    assert body["scopes_supported"] == [f"api://{CLIENT}/access_as_user"]
    assert "secret" not in response.get_body().decode().lower()
    monkeypatch.delenv("MCP_ENTRA_CLIENT_SECRET")
    function_app._security.cache_clear()
    response = function_app.mcp_http(http_request(function_app, tool_message(), signed_in=False))
    assert response.status_code == 503


def test_nonprotected_deployments_keep_function_key_and_generic_documents(
    settings, modules, monkeypatch,
):
    monkeypatch.delenv("COSMOS_SHAREPOINT_PROTECTED_CONTAINERS")
    spec = importlib.util.spec_from_file_location("unprotected_cosmos_mcp", MCP_PATH / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    client = SimpleNamespace(query_items=lambda **_: [{"id": "legacy", "body": "existing content"}])
    monkeypatch.setattr(module, "_container", lambda _: ("public", client))
    assert json.loads(module._list_documents())["documents"] == [{"id": "legacy", "body": "existing content"}]
    http_function = next(f for f in module.app.get_functions() if f.get_function_name() == "mcp_http")
    assert http_function.get_bindings()[0].auth_level == module.func.AuthLevel.FUNCTION
