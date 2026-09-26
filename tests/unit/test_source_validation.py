"""Source connectivity warnings must not expose internal connector failures."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type,connector,probe_name", [
    ("azure-blob", "blob_connector", "test_blob_connection"),
    ("cosmosdb", "cosmosdb_connector", "test_cosmosdb_connection"),
    ("sharepoint", None, "_test_sharepoint_connection"),
])
@pytest.mark.parametrize("raises", [False, True])
async def test_source_warning_does_not_expose_connector_error(
    api_app, monkeypatch, caplog, source_type, connector, probe_name, raises
):
    api = sys.modules["api"]
    internal_detail = "sensitive-internal-connector-detail"
    probe = AsyncMock(
        side_effect=RuntimeError(internal_detail) if raises else None,
        return_value=(False, internal_detail),
    )
    if connector:
        module_name = f"connectors.{connector}"
        module = ModuleType(module_name)
        setattr(module, probe_name, probe)
        monkeypatch.setitem(sys.modules, module_name, module)
    else:
        monkeypatch.setattr(api, probe_name, probe)
    saved = []
    store = SimpleNamespace(list=lambda kind: [], upsert=saved.append)
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "_require_blob_source_enabled", lambda *args: None)
    configs = {
        "azure-blob": {"container": "documents"},
        "cosmosdb": {
            "endpoint": "https://example.documents.azure.com:443/",
            "database": "db",
            "container": "items",
        },
        "sharepoint": {"site_id": "test-site", "drive_id": "test-drive"},
    }
    request = api.CreateSourceRequest(
        name="source-validation-test",
        type=source_type,
        config=configs[source_type],
    )

    result = await api.create_source(request)

    probe.assert_awaited_once()
    assert result["success"] is True
    assert result["source"].enabled is True
    assert len(saved) == 1
    assert len(result["warnings"]) == 1
    assert internal_detail not in str(result)
    assert internal_detail not in caplog.text
    if raises:
        assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_invalid_sharepoint_probe_does_not_echo_validation_input(api_app):
    api = sys.modules["api"]
    ok, result = await api._test_sharepoint_connection({
        "site_id": "test-site",
        "drive_id": "test-drive",
        "poll_interval_seconds": "sensitive-internal-detail",
    })
    assert ok is False
    assert result.startswith("Invalid SharePoint configuration.")
    assert "sensitive-internal-detail" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination_type,module_name,probe_name",
    [
        ("cosmosdb-vector", "connectors.cosmosdb_vector_connector", "test_vector_connection"),
        ("pgvector", "connectors.postgres_connector", "test_destination_connection"),
        ("mssql", "pyodbc", "connect"),
    ],
)
async def test_destination_warning_does_not_expose_probe_error(
    api_app, monkeypatch, caplog, destination_type, module_name, probe_name
):
    api = sys.modules["api"]
    internal_detail = "sensitive-destination-probe-detail"
    module = ModuleType(module_name)
    if destination_type == "mssql":
        setattr(module, probe_name, lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(internal_detail)
        ))
    else:
        setattr(module, probe_name, AsyncMock(side_effect=RuntimeError(internal_detail)))
    monkeypatch.setitem(sys.modules, module_name, module)
    saved = []
    store = SimpleNamespace(list=lambda kind: [], upsert=saved.append)
    monkeypatch.setattr(api, "get_store", lambda: store)

    result = await api.create_destination(api.CreateDestinationRequest(
        name=f"{destination_type}-validation-test",
        type=destination_type,
        config={"server": "test", "database": "test", "table": "vectors"},
    ))

    assert result["success"] is True
    assert len(saved) == 1
    assert len(result["warnings"]) == 1
    assert internal_detail not in str(result)
    assert internal_detail not in caplog.text
    assert "RuntimeError" in caplog.text
