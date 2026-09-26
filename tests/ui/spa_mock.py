"""Deterministic mock API responses for the redesigned SPA (web/static/index.html)."""
from __future__ import annotations

import copy
from typing import Any

PIPELINE_COUNT = 23

SOURCES = [
    {"id": "src-docs", "name": "Contracts blob", "type": "azure-blob", "enabled": True,
     "config": {"account_url": "https://contoso.blob.core.windows.net", "container": "contracts"}},
    {"id": "src-orders", "name": "Orders feed", "type": "cosmosdb", "enabled": True,
     "config": {"endpoint": "https://orders.documents.azure.com", "database": "sales", "container": "orders"}},
]

DESTINATIONS = [
    {"id": "dst-vec", "name": "Vector store A", "type": "cosmosdb-vector",
     "config": {"endpoint": "https://vec.documents.azure.com", "database": "rag", "container": "chunks"}},
]

EMBED_MODEL = {"id": "mdl-embed", "name": "text-embedding-3-small", "deployment": "text-embedding-3-small",
               "type": "azure-openai", "model_category": "embedding", "dimensions": 1536,
               "endpoint": "https://aoai.openai.azure.com"}
CHAT_MODEL = {"id": "mdl-chat", "name": "gpt-4o", "deployment": "gpt-4o", "type": "azure-openai",
              "model_category": "chat", "endpoint": "https://aoai.openai.azure.com"}


def _pipeline(i: int) -> dict[str, Any]:
    return {
        "id": f"pip-{i:03d}", "name": f"Pipeline {i:03d}", "status": "active",
        "sources": [{"source_id": "src-docs" if i % 2 else "src-orders"}],
        "destination_id": "dst-vec", "docgrok_pipeline": "mdl-embed",
        "stats": {"source_doc_count": 100, "embedded_count": 100, "lifetime_embedded_count": 1000 + i},
    }


def default_responses() -> dict[str, Any]:
    """Map of API path (without query) to JSON payload."""
    pipelines = [_pipeline(i) for i in range(1, PIPELINE_COUNT + 1)]
    return copy.deepcopy({
        "/api/auth/login": {"name": "admin", "role": "admin", "is_admin": True, "permissions": ["*"]},
        "/api/sources": {"sources": SOURCES},
        "/api/destinations": {"destinations": DESTINATIONS},
        "/api/pipelines": {"pipelines": pipelines},
        "/api/models": {"models": [EMBED_MODEL, CHAT_MODEL]},
        "/api/health/checks": {"overall": "healthy", "pipelines": [], "sources": [], "destinations": [], "models": []},
        "/api/triggers/status": {"blob_sources": [{"id": "src-docs", "name": "Contracts blob", "status": "configured"}]},
        "/api/operations/resource-insights": {"pipelines": []},
        "/api/metrics/live": {"pipelines": {}},
        "/api/operations/deployments": {"deployments": [
            {"name": "omnivec-worker", "replicas": 2, "ready_replicas": 2, "autoscaling": {"max_replicas": 10}}]},
        "/api/stats": {},
        "/api/capabilities": {},
        "/api/metrics/timeseries": {"source": "app_insights", "granularity": "hour", "buckets": [
            {"t": "2026-09-26T10:00:00Z", "processed": 1200, "failed": 0, "latency_ms": 350},
            {"t": "2026-09-26T11:00:00Z", "processed": 1800, "failed": 0, "latency_ms": 320}]},
        "/api/metrics": {"events_processed": 3000, "events_failed": 0, "source": "app_insights"},
        "/api/docgrok/transforms": {"transforms": [
            {"name": "pdf-layout", "description": "Extract layout from PDFs, chunk by section and embed",
             "file_types": ["pdf"], "steps": [{"type": "extract"}, {"type": "chunk"}, {"type": "embed"}]}]},
        "/api/cloud-deployments": {"deployments": []},
        "/api/auth/tokens": {"tokens": []},
    })
