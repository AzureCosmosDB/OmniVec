"""Contracts for best-effort batch telemetry without Cosmos metrics persistence."""
import asyncio
import logging
import sys
from unittest.mock import Mock

import pytest
from fastapi import HTTPException


@pytest.fixture
def api(api_app, monkeypatch):
    module = sys.modules["api"]
    import telemetry
    telemetry.reset_batch_dedup()
    telemetry.metrics_store.reset()
    module.batch_metrics.invalidate()
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(module, "get_store", Mock(side_effect=AssertionError("unexpected DB access")))
    return module


def test_inline_report_is_local_and_deduplicated(api):
    payload = {"batch_key": "generation:batch", "processed": 10}
    assert api.report_inline_metrics("pipeline", payload) == {"ok": True}
    assert api.report_inline_metrics("pipeline", payload) == {"ok": True, "dedup": True}
    assert api.metrics_store.snapshot()["documents_embedded"] == 10
    api.get_store.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"batch_key": ""},
    {"batch_key": "x" * 513},
    {"batch_key": "batch", "processed": float("inf")},
    {"batch_key": "batch", "processing_time_ms": float("nan")},
    {"batch_key": "batch", "failed": -1},
    {"batch_key": "batch", "resource_weight": -1},
    {"batch_key": "batch", "reported_at": "invalid"},
])
def test_invalid_reports_fail_explicitly(api, payload):
    with pytest.raises(HTTPException) as error:
        api.report_inline_metrics("pipeline", payload)
    assert error.value.status_code == 400


def test_configured_but_unavailable_exporter_returns_503(api, monkeypatch):
    import telemetry
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "configured")
    monkeypatch.setattr(telemetry, "_initialized", False)
    with pytest.raises(HTTPException) as error:
        api.report_inline_metrics("pipeline", {"batch_key": "batch"})
    assert error.value.status_code == 503


def test_custom_event_contains_original_report_time(api, monkeypatch):
    import telemetry
    emitted = []
    class Capture(logging.Handler):
        def emit(self, record):
            emitted.append(record)
    monkeypatch.setattr(telemetry, "_initialized", True)
    monkeypatch.setattr(telemetry.batch_logger, "handlers", [Capture()])
    monkeypatch.setattr(telemetry.batch_logger, "level", logging.INFO)
    api.report_inline_metrics("pipeline", {
        "batch_key": "batch", "processed": 2, "reported_at": "2026-10-06T10:00:00Z"})
    assert len(emitted) == 1
    assert getattr(emitted[0], "microsoft.custom_event.name") == "omnivec.embedding.batch"
    assert emitted[0].reported_at == "2026-10-06T10:00:00Z"
    assert emitted[0].processed == 2


def test_history_unavailable_never_falls_back_to_cosmos(api, monkeypatch):
    monkeypatch.setattr(api, "_run_kql", lambda *args: None)
    result = asyncio.run(api.get_metrics_timeseries())
    assert result["buckets"] == []
    assert result["source"] == "unavailable"
    api.get_store.assert_not_called()


def test_query_deduplicates_before_summary_and_applies_reset(api):
    queries = []
    def query(kql, timespan):
        queries.append(api.batch_metrics.BATCHES_KQL + kql)
        return []
    assert api.batch_metrics.pipeline_summary(query, "pipeline", "2026-10-06T10:00:00Z") == {}
    text = queries[0]
    assert text.index("arg_max(TimeGenerated, *)") < text.index("processed=sum(processed)")
    assert "by pipeline_id, batch_key" in text
    assert "timestamp >= datetime(2026-10-06T10:00:00+00:00)" in text
