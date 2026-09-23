import asyncio
import importlib.util
from pathlib import Path
import shlex

import pytest
from azure.core.exceptions import HttpResponseError


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("permission_guidance", ROOT / "api" / "permission_guidance.py")
guidance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guidance)
SUB = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
RID = f"/subscriptions/{SUB}/resourceGroups/customer-rg/providers/Microsoft.DocumentDB/databaseAccounts/customer-account"
CONFIG = {"endpoint": "https://customer-account.documents.azure.com", "database": "orders", "container": "documents"}


def denied(message="Forbidden"):
    error = HttpResponseError(message=message)
    error.status_code = 403
    return error


@pytest.mark.parametrize("kind,connector,role", [
    ("source", "cosmosdb", "Cosmos DB Built-in Data Reader"),
    ("destination", "cosmosdb-vector", "Cosmos DB Built-in Data Contributor"),
])
def test_exact_scoped_deterministic_commands(kind, connector, role):
    plan = guidance.grant_commands(kind, connector, CONFIG, RID, PRINCIPAL)
    assert plan == guidance.grant_commands(kind, connector, CONFIG, RID, PRINCIPAL)
    assert plan["role"] == role
    args = shlex.split(plan["commands"]["bash"])
    assert args[args.index("--subscription") + 1] == SUB
    assert args[args.index("--resource-group") + 1] == "customer-rg"
    assert args[args.index("--principal-id") + 1] == PRINCIPAL
    assert args[args.index("--scope") + 1] == "/dbs/orders/colls/documents"
    assert args[args.index("--role-definition-id") + 1].startswith(RID + "/sqlRoleDefinitions/")
    assert "YOUR_" not in str(plan) and "RESOURCE_GROUP" not in str(plan)


def test_blob_command_is_container_scoped():
    rid = RID.replace("Microsoft.DocumentDB/databaseAccounts", "Microsoft.Storage/storageAccounts")
    config = {"account_url": "https://customer-account.blob.core.windows.net", "container": "documents"}
    plan = guidance.grant_commands("source", "azure-blob", config, rid, PRINCIPAL)
    args = shlex.split(plan["commands"]["bash"])
    assert args[args.index("--scope") + 1] == rid + "/blobServices/default/containers/documents"
    assert "--assignee-object-id" in args and "--assignee" not in args


@pytest.mark.parametrize("value", ["../other", "a/b", "a\nb", "a?b", "a#b", ""])
def test_invalid_scope_never_generates_command(value):
    with pytest.raises(ValueError):
        guidance.grant_commands("source", "cosmosdb", {**CONFIG, "database": value}, RID, PRINCIPAL)


def test_shell_quoting_is_not_interpolation():
    args = ["az", "value", "space and 'quote'", "$(echo attack); & echo"]
    assert shlex.split(guidance.render_command(args, "bash")) == args
    assert "'space and ''quote'''" in guidance.render_command(args, "powershell")
    assert "'$(echo attack); & echo'" in guidance.render_command(args, "powershell")


@pytest.mark.parametrize("endpoint", [
    "http://customer-account.documents.azure.com", "https://evil.example",
    "https://customer-account.documents.azure.com.evil.example",
    "https://user:secret@customer-account.documents.azure.com",
    "https://customer-account.documents.azure.com?sig=secret",
])
def test_endpoint_validation_before_probing(endpoint):
    with pytest.raises(ValueError):
        guidance.account_from_config("cosmosdb", {"endpoint": endpoint})


def test_mismatched_account_rejected():
    with pytest.raises(ValueError):
        guidance.grant_commands("source", "cosmosdb", CONFIG, RID.replace("customer-account", "other"), PRINCIPAL)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AZURE_PRINCIPAL_ID", PRINCIPAL)
    monkeypatch.setattr(guidance, "probe", lambda *args: None)
    return monkeypatch


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("secret"), "network_unreachable"),
    (denied("403 Forbidden credential=secret"), "access_denied"),
    (denied("403 Substatus 5300 cannot be authorized in data plane secret"), "provisioning_required"),
    (denied("403 Forbidden by firewall secret"), "network_restricted"),
    (HttpResponseError(message="AADSTS700213 secret"), "authentication_failed"),
])
def test_failures_keep_classification_and_hide_secrets(configured, error, expected):
    def fail(*args):
        raise error
    configured.setattr(guidance, "probe", fail)
    result = asyncio.run(guidance.check_access("source", "cosmosdb", CONFIG, RID))
    assert result["status"] == expected
    assert "secret" not in str(result)
    assert bool(result["commands"]) == (expected == "access_denied")


def test_missing_ids_never_produces_placeholder_command(configured):
    configured.delenv("AZURE_PRINCIPAL_ID")
    def fail(*args):
        raise denied()
    async def missing(*args):
        return "", "Paste the account resource ID."
    configured.setattr(guidance, "probe", fail)
    configured.setattr(guidance, "resolve_resource_id", missing)
    result = asyncio.run(guidance.check_access("source", "cosmosdb", CONFIG))
    assert result["status"] == "access_denied" and not result["commands"]
    assert len(result["missing"]) == 2


def test_destination_read_is_not_write_verification(configured):
    result = asyncio.run(guidance.check_access("destination", "cosmosdb-vector", CONFIG, RID))
    assert result["status"] == "read_verified"
    assert "not yet verified" in result["summary"]
    assert not result["commands"]


def test_wrong_principal_rejected_before_probe(configured):
    result = asyncio.run(guidance.check_access("source", "cosmosdb", CONFIG, RID, SUB))
    assert result["status"] == "needs_input"
    assert not result["commands"]


def test_unknown_connector_does_not_claim_access(configured):
    result = asyncio.run(guidance.check_access("source", "sharepoint", {}))
    assert result["status"] == "unsupported"
    assert not result["commands"]


def test_unexpected_programming_failure_not_hidden(configured):
    def fail(*args):
        raise TypeError("bug")
    configured.setattr(guidance, "probe", fail)
    with pytest.raises(TypeError):
        asyncio.run(guidance.check_access("source", "cosmosdb", CONFIG, RID))


def test_endpoint_uses_saved_configuration_and_requires_auth(api_app, monkeypatch):
    import sys
    from fastapi.testclient import TestClient

    captured = []
    async def check(*args):
        captured.append(args)
        return {"status": "read_verified", "summary": "Read verified"}
    monkeypatch.setitem(sys.modules, "permission_guidance", guidance)
    monkeypatch.setattr(guidance, "check_access", check)
    class Store:
        def get(self, resource_ref, kind):
            assert resource_ref == "src-owned" and kind == "source"
            return {"type": "cosmosdb", "config": CONFIG}
    monkeypatch.setattr(sys.modules["api"], "get_store", lambda: Store())
    client = TestClient(api_app)
    body = {"kind": "source", "resource_ref": "src-owned", "config": {"endpoint": "https://wrong.invalid"}}
    assert client.post("/api/permissions/check", json=body).status_code == 401
    result = client.post("/api/permissions/check", json=body, headers={"Authorization": "Bearer test-admin-token"})
    assert result.status_code == 200
    assert captured == [("source", "cosmosdb", CONFIG, "", "")]
    assert client.post("/api/permissions/check", json={"kind": "invalid"},
                       headers={"Authorization": "Bearer test-admin-token"}).status_code == 422
