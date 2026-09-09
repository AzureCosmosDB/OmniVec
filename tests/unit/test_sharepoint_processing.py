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
