"""Local SharePoint extraction and configuration contracts; no Azure calls."""
import base64
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def processor(monkeypatch):
    directory = ROOT / "docgrok" / "pipeline-worker"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("sharepoint_test_worker", directory / "worker.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    # Extraction scratch files stay in the repository and are removed below.
    monkeypatch.setattr(module.tempfile, "tempdir", str(ROOT / "tests" / "sharepoint"))
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["txt", "md", "csv", "json", "html", "xml", "docx", "pdf"])
async def test_supported_file_extraction(processor, extension):
    text = "SharePoint document with sufficient embedded text for extraction without OCR."
    if extension == "docx":
        from docx import Document
        document = Document()
        document.add_paragraph(text)
        buffer = io.BytesIO()
        document.save(buffer)
        payload = buffer.getvalue()
    elif extension == "pdf":
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((50, 50), text, fontsize=10)
            payload = document.tobytes()
    else:
        payload = text.encode()
    context = {
        "source_kind": "inline_b64", "source_name": "example." + extension,
        "inline_b64": base64.b64encode(payload).decode("ascii"),
    }
    try:
        await processor._stage_extract(SimpleNamespace(notes=[], output={}), context, {"ocr_engine": "none"})
        assert text in "\n".join(context["page_texts"])
    finally:
        if context.get("pdf_path"):
            Path(context["pdf_path"]).unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,config,reason", [
    (" \n ", {}, "empty_document"),
    ("short", {"min_chars": 100}, "all_chunks_filtered"),
])
async def test_empty_or_filtered_document_skips_without_placeholder(processor, text, config, reason):
    context = {"page_texts": [text]}
    step = SimpleNamespace(notes=[], output={})
    await processor._stage_chunk(step, context, config)
    assert context["chunks"] == []
    assert context["_skip_pipeline"] is True
    assert context["_skip_reason"] == reason
    result = await processor._execute_pipeline(processor.PipelineRunner("test"), {"stages": []}, context)
    assert result["skipped"] is True and result["skip_reason"] == reason and result["chunks"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks,embeddings,expected", [
    ([], [], None), (["a", "b"], [[0.1]], None), (["a"], [[]], None),
    (["a"], [[float("nan")]], None), (["a"], [[0.1]], 2),
])
async def test_incomplete_embeddings_are_errors_not_success(processor, chunks, embeddings, expected):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        await processor._execute_pipeline(processor.PipelineRunner("test"), {"stages": []}, {
            "chunks": chunks, "embeddings": embeddings, "expected_dim": expected,
        })
    assert failure.value.status_code == 502


def test_sharepoint_destination_requires_transaction_compatible_partition(api_app):
    from fastapi import HTTPException
    api = sys.modules["api"]
    store = SimpleNamespace(get=lambda *_: {"type": "sharepoint"})
    request = SimpleNamespace(
        sources=[SimpleNamespace(source_id="source")], processing_mode="queue",
        vector_index_path="embedding", content_field=None, store_content=True,
    )
    destination = {"type": "cosmosdb-vector", "config": {"partition_key_path": "/document_id"}}
    api._require_sharepoint_compatible(store, request, destination)
    for path in ("/id", "/source_id", "/_omnivec_sync", "/embedding", ""):
        destination["config"]["partition_key_path"] = path
        with pytest.raises(HTTPException):
            api._require_sharepoint_compatible(store, request, destination)
    destination["config"]["partition_key_path"] = "/document_id"
    request.content_field = "_omnivec_sync"
    with pytest.raises(HTTPException):
        api._require_sharepoint_compatible(store, request, destination)
    # Existing non-SharePoint pipeline configurations remain unaffected.
    api._require_sharepoint_compatible(SimpleNamespace(get=lambda *_: {"type": "cosmosdb"}), request, destination)


def test_sharepoint_identity_is_rejected_for_non_sharepoint_source(api_app):
    from fastapi import HTTPException
    api = sys.modules["api"]
    store = SimpleNamespace(get=lambda *_: {"type": "azure-blob"})
    request = SimpleNamespace(
        sources=[SimpleNamespace(
            source_id="source",
            sharepoint_identity=SimpleNamespace(
                tenant_id="11111111-1111-1111-1111-111111111111",
                client_id="22222222-2222-2222-2222-222222222222",
            ),
        )],
        processing_mode="queue",
        vector_index_path="embedding",
        content_field=None,
        store_content=True,
    )
    with pytest.raises(HTTPException, match="only be configured for SharePoint"):
        api._require_sharepoint_compatible(
            store, request,
            {"type": "cosmosdb-vector", "config": {"partition_key_path": "/document_id"}},
        )


def test_pipeline_identities_are_rejected_for_wrong_source_types(api_app):
    from fastapi import HTTPException
    api = sys.modules["api"]
    identity = SimpleNamespace(
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
    )
    request = SimpleNamespace(sources=[SimpleNamespace(
        source_id="source", sharepoint_identity=None, onelake_identity=identity,
    )])
    with pytest.raises(HTTPException, match="only be configured for OneLake"):
        api._require_pipeline_source_identities(
            SimpleNamespace(get=lambda *_: {"type": "azure-blob"}), request
        )
    api._require_pipeline_source_identities(
        SimpleNamespace(get=lambda *_: {"type": "onelake-iceberg"}), request
    )


@pytest.mark.asyncio
async def test_full_source_sync_bumps_pipeline_generation(api_app, monkeypatch):
    api = sys.modules["api"]
    pipeline = SimpleNamespace(
        id="pipeline", status=api.PipelineStatus.ACTIVE,
        sources=[SimpleNamespace(source_id="source")],
        destination_id="destination", docgrok_pipeline="model",
        generation="old", reset_at=None, updated_at=None,
    )
    saved = []
    store = SimpleNamespace(
        get=lambda *_: {"id": "source"},
        list=lambda kind: [{"id": "pipeline"}] if kind == "pipeline" else [],
        upsert=lambda doc: saved.append(doc),
    )
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "_pipeline_from_doc", lambda _: pipeline)
    monkeypatch.setattr(api, "_to_doc", lambda value, _: value)
    result = await api.sync_source("source", api.SyncSourceRequest(full_sync=True))
    assert result["success"] is True
    assert "full replay" in result["message"]
    assert pipeline.reset_at is not None
    assert pipeline.generation != "old"
    assert saved[0] is pipeline
    assert saved[1]["doc_type"] == "sync_operation"
    assert saved[1]["id"] == result["operation_id"]
    assert saved[1]["pipeline_ids"] == ["pipeline"]
    assert result["status_url"].endswith(result["operation_id"])


@pytest.mark.parametrize(
    ("embedded", "failed", "expected"),
    [(0, 0, "running"), (2, 0, "ready"), (2, 1, "failed")],
)
def test_source_sync_status_tracks_current_pipeline_readiness(
    api_app, monkeypatch, embedded, failed, expected
):
    api = sys.modules["api"]
    operation = {
        "id": "sync-test",
        "doc_type": "sync_operation",
        "source_id": "source",
        "pipeline_ids": ["pipeline"],
        "full_sync": True,
        "minimum_documents": 2,
        "status": "running",
        "started_at": "2026-01-01T00:00:00",
        "reset_at": "2026-01-01T00:00:00",
    }
    saved = []
    store = SimpleNamespace(
        get=lambda *_: operation,
        upsert=lambda doc: saved.append(doc.copy()),
    )

    class Stats:
        embedded_count = embedded
        jobs = SimpleNamespace(failed=failed)

        def model_dump(self, **_):
            return {
                "embedded_count": self.embedded_count,
                "jobs": {"failed": self.jobs.failed},
            }

    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "get_pipeline_stats", lambda _: Stats())

    result = api.get_source_sync("sync-test")

    assert result["status"] == expected
    assert result["pipelines"][0]["ready"] is (embedded >= 2)
    assert bool(saved) is (expected != "running")


@pytest.mark.asyncio
async def test_scoped_search_selects_exact_pipeline_and_parameterizes_filters(
    api_app, monkeypatch
):
    api = sys.modules["api"]
    destination = {
        "id": "destination",
        "type": "cosmosdb-vector",
        "config": {
            "endpoint": "https://example.documents.azure.com",
            "database": "db",
            "container": "vectors",
            "vector_dimensions": 1536,
        },
    }
    pipelines = [
        {
            "id": "pipeline-other",
            "destination_id": "destination",
            "status": "active",
            "docgrok_pipeline": "other-model",
            "vector_index_path": "/embedding",
            "sources": [{"source_id": "other", "content_fields": ["content"]}],
        },
        {
            "id": "pipeline-exact",
            "destination_id": "destination",
            "status": "active",
            "docgrok_pipeline": "exact-model",
            "vector_index_path": "/embedding",
            "sources": [{"source_id": "source", "content_fields": ["content"]}],
        },
    ]
    store = SimpleNamespace(
        list=lambda kind: pipelines if kind == "pipeline" else [],
        get=lambda item_id, kind: destination
        if kind == "destination" and item_id == "destination"
        else None,
    )
    monkeypatch.setattr(api, "get_store", lambda: store)
    document_filter = {
        "where": "c.source_id = @source_id AND c.pipeline_id = @pipeline_id",
        "params": {"source_id": "source", "pipeline_id": "pipeline-exact"},
    }

    indexes, warnings = await api._build_index_specs(
        ["destination"],
        pipeline_id="pipeline-exact",
        document_filter=document_filter,
    )

    assert warnings == []
    assert len(indexes) == 1
    assert indexes[0]["pipeline_id"] == "pipeline-exact"
    assert indexes[0]["embedding"]["pipeline"] == "exact-model"
    assert indexes[0]["filter"] == document_filter
    assert indexes[0]["return_fields"] == ["source_id", "pipeline_id"]


@pytest.mark.asyncio
async def test_scoped_search_rejects_unsupported_destination(api_app, monkeypatch):
    api = sys.modules["api"]
    destination = {
        "id": "destination",
        "type": "pgvector",
        "config": {"host": "db", "database": "db", "table": "vectors"},
    }
    pipeline = {
        "id": "pipeline",
        "destination_id": "destination",
        "status": "active",
        "docgrok_pipeline": "model",
        "sources": [{"source_id": "source", "content_fields": ["content"]}],
    }
    store = SimpleNamespace(
        list=lambda kind: [pipeline] if kind == "pipeline" else [],
        get=lambda *_: destination,
    )
    monkeypatch.setattr(api, "get_store", lambda: store)

    indexes, warnings = await api._build_index_specs(
        ["destination"],
        pipeline_id="pipeline",
        document_filter={
            "where": "c.source_id = @source_id",
            "params": {"source_id": "source"},
        },
    )

    assert indexes == []
    assert warnings == [
        "destination destination does not support OmniVec readiness filters"
    ]


@pytest.mark.asyncio
async def test_capabilities_are_authoritative_and_unsupported_sources_fail_closed(api_app, monkeypatch):
    api = sys.modules["api"]
    monkeypatch.setattr(api, "_BLOB_SOURCE_ENABLED", True)
    capabilities = await api.get_capabilities()
    assert capabilities["allowed_source_types"] == [
        "azure-blob", "cosmosdb", "postgresql", "mssql",
        "databricks", "onelake-iceberg", "sharepoint",
    ]
    assert capabilities["unsupported_source_types"] == ["s3", "http"]
    monkeypatch.setattr(api, "get_store", lambda: SimpleNamespace(list=lambda _: []))
    for source_type in (api.SourceType.S3, api.SourceType.HTTP):
        with pytest.raises(Exception) as error:
            await api.create_source(api.CreateSourceRequest(
                name=f"unsupported-{source_type.value}", type=source_type, config={},
            ))
        assert error.value.status_code == 422
