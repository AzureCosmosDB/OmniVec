"""Offline tests for querying workspace-based Application Insights (AppMetrics)."""
import asyncio
import math
import sys
from types import SimpleNamespace

import pytest


SUCCESS = "Success"


@pytest.fixture
def api(api_app, monkeypatch):
    # azure-monitor-query is only installed in the API image.
    monkeypatch.setitem(sys.modules, "azure.monitor.query",
                        SimpleNamespace(LogsQueryStatus=SimpleNamespace(SUCCESS=SUCCESS)))
    return sys.modules["api"]


class FakeLogsClient:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query_workspace(self, ws_id, kql, timespan=None):
        self.queries.append(kql)
        return SimpleNamespace(status=SUCCESS, tables=[SimpleNamespace(rows=self.rows)])


def test_kql_cell_normalizes_numbers_and_nan(api):
    assert api._kql_cell("110025") == 110025.0
    assert api._kql_cell("17185.15") == pytest.approx(17185.15)
    assert api._kql_cell(math.nan) is None
    assert api._kql_cell("pip-123") == "pip-123"
    assert api._kql_cell(7) == 7
    assert api._kql_cell(None) is None


def test_run_kql_maps_custom_metrics_onto_appmetrics(api, monkeypatch):
    client = FakeLogsClient([["embedded", "42", math.nan]])
    monkeypatch.setattr(api, "_get_logs_client", lambda: (client, "ws"))

    rows = api._run_kql("customMetrics | where name == 'omnivec.documents.embedded' | summarize sum(value)")

    assert rows == [["embedded", 42.0, None]]
    assert client.queries[0].startswith("let customMetrics = AppMetrics")


def test_run_kql_leaves_other_queries_untouched(api, monkeypatch):
    client = FakeLogsClient([])
    monkeypatch.setattr(api, "_get_logs_client", lambda: (client, "ws"))

    api._run_kql("AppRequests | count")

    assert client.queries == ["AppRequests | count"]


def test_get_metrics_reads_split_union_columns(api, monkeypatch):
    # A union of real and long values comes back as metric, val_real, val_long.
    rows = [
        ["embedded", 110025.0, None],
        ["embed_lat_avg", 17185.15, None],
        ["embed_lat_cnt", None, 248.0],
    ]
    monkeypatch.setattr(api, "_run_kql", lambda *_a, **_k: rows)

    result = asyncio.run(api.get_metrics())

    assert result["source"] == "app_insights"
    assert result["events_processed"] == 110025
    assert result["latency"]["embedding"]["avg"] == pytest.approx(17185.2)
