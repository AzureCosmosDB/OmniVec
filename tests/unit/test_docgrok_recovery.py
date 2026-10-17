"""Regression tests for document-processing and model-scheduler stalls."""
import asyncio
import base64
import importlib.util
import json
import struct
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from .test_sharepoint_processing import processor


@pytest.mark.parametrize("chunk_size", [0, -1, -2000])
def test_legacy_chunker_rejects_nonprogressing_sizes(processor, chunk_size):
    with pytest.raises(HTTPException) as error:
        processor.chunk_text("Document text", chunk_size)
    assert error.value.status_code == 400


@pytest.mark.parametrize("model_name", ["ProcessRequest", "BlobProcessRequest"])
@pytest.mark.parametrize("chunk_size", [0, -1])
def test_request_rejects_invalid_chunk_size(processor, model_name, chunk_size):
    with pytest.raises(ValidationError):
        getattr(processor, model_name)(
            blob_name="example.txt", blob_container="documents", chunk_size=chunk_size,
        )


@pytest.mark.parametrize("strategy", ["recursive", "fixed", "sentence", "paragraph"])
@pytest.mark.parametrize("config", [
    {"max_chars": 0}, {"max_chars": -1}, {"overlap_chars": -1},
    {"max_chars": 10, "overlap_chars": 10}, {"min_chars": -1},
])
def test_all_chunking_strategies_validate_limits(processor, strategy, config):
    with pytest.raises(HTTPException) as error:
        processor.chunk_with_strategy("Document text", strategy=strategy, **config)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "invalid", float("inf")])
async def test_invalid_stage_sizes_report_client_error(processor, value):
    with pytest.raises(HTTPException) as error:
        await processor._stage_chunk(
            SimpleNamespace(notes=[], output={}),
            {"page_texts": ["Document text"]}, {"max_chars": value},
        )
    assert error.value.status_code == 400


def test_valid_chunking_retains_content_and_overlap(processor):
    assert processor.chunk_with_strategy(
        "abcdefgh", strategy="fixed", max_chars=4, overlap_chars=1,
    ) == ["abcd", "defgh"]


@pytest.mark.asyncio
async def test_legacy_small_chunks_adjust_builtin_overlap(processor, monkeypatch):
    async def capture_pipeline(_runner, definition, _context):
        return next(stage["config"] for stage in definition["stages"] if stage["type"] == "chunk")

    monkeypatch.setattr(processor, "_execute_pipeline", capture_pipeline)
    config = await processor._run_default_pipeline(
        pr=processor.PipelineRunner("test"), blob_container=None, blob_name=None,
        blob_account_url=None, blob_connection_string=None,
        inline_b64=None, inline_text="Document text", source_name=None,
        pipeline_hint=None, model_id="test", router_url="http://local.invalid",
        chunk_size=10,
    )
    assert config["max_chars"] == 10
    assert 0 <= config["overlap_chars"] < 10
    assert processor.chunk_with_strategy(
        "Document text", max_chars=config["max_chars"], overlap_chars=config["overlap_chars"],
    )


def router_stub(monkeypatch, processor, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(processor.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.asyncio
async def test_document_embeddings_are_batched_in_order(processor, monkeypatch):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.url.path == "/embed/batch"
        return httpx.Response(200, json={
            "outputs": [[[float(text)]] for text in payload["texts"]],
        })

    router_stub(monkeypatch, processor, handle)
    monkeypatch.setattr(processor, "EMBED_BATCH_SIZE", 4)
    result = await processor.embed_via_router([str(i) for i in range(9)], "model", "http://local.invalid")
    assert result == [[float(i)] for i in range(9)]
    assert [len(call["texts"]) for call in calls] == [4, 4, 1]
    assert all(call["model_id"] == "model" for call in calls)


@pytest.mark.asyncio
async def test_router_binary_embeddings_are_decoded_in_order(processor, monkeypatch):
    def handle(request):
        payload = json.loads(request.content)
        assert request.headers["accept"] == processor.EMBEDDING_BINARY_MEDIA_TYPE
        values = [float(text) for text in payload["texts"]]
        body = struct.pack("<4sHHII", b"OVEC", 1, 0, len(values), 2)
        body += b"".join(struct.pack("<ff", value, value + 0.5) for value in values)
        return httpx.Response(
            200,
            headers={"Content-Type": processor.EMBEDDING_BINARY_MEDIA_TYPE},
            content=body,
        )

    router_stub(monkeypatch, processor, handle)
    monkeypatch.setattr(processor, "EMBED_BATCH_SIZE", 2)
    result = await processor.embed_via_router(
        ["1", "2", "3"],
        "model",
        "http://local.invalid",
    )
    assert result == [[1.0, 1.5], [2.0, 2.5], [3.0, 3.5]]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 413, 422, 429, 503])
async def test_embedding_errors_preserve_retry_classification(processor, monkeypatch, status):
    router_stub(monkeypatch, processor, lambda _: httpx.Response(status, text="backend failure"))
    with pytest.raises(HTTPException) as error:
        await processor.embed_via_router(["text"], "model", "http://local.invalid")
    assert error.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {}, [], {"outputs": []}, {"outputs": [None]}, {"outputs": [[[]]]},
    {"outputs": [[[0.1]], [[0.2]]]},
])
async def test_incomplete_embedding_batches_fail(processor, monkeypatch, response):
    router_stub(monkeypatch, processor, lambda _: httpx.Response(200, json=response))
    with pytest.raises(HTTPException) as error:
        await processor.embed_via_router(["text"], "model", "http://local.invalid")
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_direct_embedding_batches_rotate_endpoints_and_preserve_order(processor, monkeypatch):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.host, payload))
        return httpx.Response(
            200,
            json=[[float(text)] for text in payload["inputs"]],
        )

    router_stub(monkeypatch, processor, handle)
    result = await processor.embed_direct(
        [str(i) for i in range(5)],
        "mdl-ext-harrier",
        {
            "protocol": "tei",
            "endpoints": ["http://gpu-a:8000", "http://gpu-b:8000"],
            "batch_size": 2,
            "concurrency": 2,
        },
    )

    assert result == [[float(i)] for i in range(5)]
    assert [host for host, _ in calls] == ["gpu-a", "gpu-b", "gpu-a"]
    assert [len(payload["inputs"]) for _, payload in calls] == [2, 2, 1]
    assert all(payload["truncate"] is True for _, payload in calls)


@pytest.mark.asyncio
async def test_direct_single_batches_rotate_endpoints_across_requests(processor, monkeypatch):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(request.url.host)
        return httpx.Response(
            200,
            json=[[float(text)] for text in payload["inputs"]],
        )

    router_stub(monkeypatch, processor, handle)
    route = {
        "protocol": "tei",
        "endpoints": ["http://gpu-a:8000", "http://gpu-b:8000"],
        "batch_size": 512,
    }

    assert await processor.embed_direct(["1"], "cross-request-model", route) == [[1.0]]
    assert await processor.embed_direct(["2"], "cross-request-model", route) == [[2.0]]
    assert calls == ["gpu-a", "gpu-b"]


@pytest.mark.asyncio
async def test_direct_openai_embedding_uses_standard_contract(processor, monkeypatch):
    def handle(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert payload == {
            "model": "harrier",
            "input": ["1", "2"],
            "encoding_format": "float",
        }
        return httpx.Response(200, json={
            "object": "list",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [2.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0]},
            ],
            "model": "harrier",
        })

    router_stub(monkeypatch, processor, handle)
    result = await processor.embed_direct(
        ["1", "2"],
        "mdl-ext-harrier",
        {
            "protocol": "openai",
            "model": "harrier",
            "encoding_format": "float",
            "endpoints": ["http://gpu:8000"],
            "batch_size": 2,
        },
    )
    assert result == [[1.0], [2.0]]


@pytest.mark.asyncio
async def test_direct_openai_embedding_decodes_base64_float32(processor, monkeypatch):
    encoded = base64.b64encode(struct.pack("<ff", 1.25, -2.5)).decode()

    def handle(request):
        payload = json.loads(request.content)
        assert payload["encoding_format"] == "base64"
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": encoded}],
        })

    router_stub(monkeypatch, processor, handle)
    result = await processor.embed_direct(
        ["text"],
        "mdl-ext-harrier",
        {
            "protocol": "openai",
            "endpoints": ["http://gpu:8000"],
        },
    )
    assert result == [[1.25, -2.5]]


@pytest.mark.asyncio
async def test_worker_openai_endpoint_returns_binary_embeddings(processor, monkeypatch):
    async def fake_embed(texts, model_id, router_url, **kwargs):
        assert texts == ["first", "second"]
        assert model_id == "mdl-ext-harrier"
        assert router_url == processor.DOCGROK_ROUTER_URL
        return [[1.0, 1.5], [2.0, 2.5]]

    monkeypatch.setattr(processor, "embed_texts", fake_embed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=processor.app),
        base_url="http://worker",
    ) as client:
        response = await client.post(
            "/v1/embeddings",
            headers={"Accept": processor.EMBEDDING_BINARY_MEDIA_TYPE},
            json={
                "model": "mdl-ext-harrier",
                "input": ["first", "second"],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(processor.EMBEDDING_BINARY_MEDIA_TYPE)
    assert processor._decode_binary_embeddings(response.content, 2) == [
        [1.0, 1.5],
        [2.0, 2.5],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", [None, "float", "base64"])
async def test_worker_openai_json_encoding_preserves_order(processor, monkeypatch, encoding):
    vectors = [[1.0, -1.5], [2.0, 2.5]]

    async def embed(texts, model_id, router_url, **kwargs):
        assert texts == ["first", "second"]
        return vectors

    monkeypatch.setattr(processor, "embed_texts", embed)
    body = {"input": ["first", "second"], "model": "test"}
    if encoding is not None:
        body["encoding_format"] = encoding
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=processor.app), base_url="http://worker") as client:
        response = await client.post("/v1/embeddings", json=body)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [entry["index"] for entry in data] == [0, 1]
    embeddings = [entry["embedding"] for entry in data]
    if encoding == "base64":
        embeddings = [list(struct.unpack("<2f", base64.b64decode(value, validate=True))) for value in embeddings]
    assert embeddings == vectors


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], None, "text", {"input": "text", "encoding_format": "unsupported"},
                                  {"input": "text", "encoding_format": None}])
async def test_worker_openai_rejects_invalid_json_contract_before_inference(processor, monkeypatch, body):
    async def no_inference(*args, **kwargs):
        pytest.fail("Invalid request reached inference")

    monkeypatch.setattr(processor, "embed_texts", no_inference)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=processor.app), base_url="http://worker") as client:
        response = await client.post("/v1/embeddings", content=json.dumps(body),
                                     headers={"Content-Type": "application/json"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_direct_embedding_failure_falls_back_to_router(processor, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.host)
        if request.url.host == "gpu":
            return httpx.Response(503, text="unavailable")
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"outputs": [[[float(text)]] for text in payload["texts"]]},
        )

    router_stub(monkeypatch, processor, handle)
    monkeypatch.setattr(processor, "DIRECT_EMBEDDING_ROUTES", {
        "mdl-ext-harrier": {
            "protocol": "tei",
            "endpoints": ["http://gpu:8000"],
        },
    })

    result = await processor.embed_texts(
        ["1", "2"],
        "mdl-ext-harrier",
        "http://router",
        mode_override="direct-with-router-fallback",
    )

    assert result == [[1.0], [2.0]]
    assert calls == ["gpu", "router"]


@pytest.mark.asyncio
async def test_unmapped_external_model_keeps_router_path(processor, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"outputs": [[[0.5]]]})

    router_stub(monkeypatch, processor, handle)
    monkeypatch.setattr(processor, "DIRECT_EMBEDDING_ROUTES", {
        "mdl-ext-harrier": "http://gpu:8000",
    })

    result = await processor.embed_texts(
        ["text"],
        "mdl-ext-openai",
        "http://router",
        mode_override="direct-with-router-fallback",
    )

    assert result == [[0.5]]
    assert calls == ["router"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_extraction_removes_scratch_file(processor, monkeypatch, tmp_path, cancelled):
    monkeypatch.setattr(processor.tempfile, "tempdir", str(tmp_path))
    scratch = tmp_path / "incomplete.pdf"
    scratch.write_bytes(b"%PDF incomplete")

    async def failing_extract(_step, context, _config):
        context["pdf_path"] = str(scratch)
        if cancelled:
            raise asyncio.CancelledError()
        raise HTTPException(status_code=400, detail="Invalid PDF")

    monkeypatch.setitem(processor.STAGE_HANDLERS, "extract", failing_extract)
    with pytest.raises(asyncio.CancelledError if cancelled else HTTPException):
        await processor._execute_pipeline(
            processor.PipelineRunner("test"), {"stages": [{"type": "extract"}]}, {},
        )
    assert not scratch.exists()


@pytest.fixture
def clip_module(monkeypatch):
    # No GPU/model dependencies or downloads are needed to run the real scheduler.
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False), Tensor=object,
    ))
    path = Path(__file__).resolve().parents[2] / "docgrok" / "services" / "embedding" / "clip" / "api.py"
    spec = importlib.util.spec_from_file_location("clip_recovery_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_DYN_BATCH_DISABLE", False)
    monkeypatch.setattr(module, "_DYN_BATCH_WAIT_MS", 0)
    monkeypatch.setattr(module, "_DYN_BATCH_MAX", 1)
    monkeypatch.setattr(module, "get_image_embedding", lambda images: SimpleNamespace(
        cpu=lambda: SimpleNamespace(tolist=lambda: [[float(image)] for image in images]),
    ))
    yield module
    module._DOWNLOAD_POOL.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_clip_request_does_not_kill_scheduler(clip_module):
    scheduler = asyncio.create_task(clip_module._scheduler_loop())
    try:
        await asyncio.sleep(0)
        cancelled = clip_module._ImageBatchItem([1])
        cancelled.future.cancel()
        await clip_module._image_queue.put(cancelled)
        result = await asyncio.wait_for(clip_module.embed_images_batched([2]), timeout=2)
        assert result == [[2.0]]
        assert not scheduler.done()
        assert await asyncio.wait_for(clip_module.embed_images_batched([3]), timeout=2) == [[3.0]]
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_clip_batch_preserves_other_result_offsets(clip_module, monkeypatch):
    monkeypatch.setattr(clip_module, "_DYN_BATCH_MAX", 3)
    monkeypatch.setattr(clip_module, "_DYN_BATCH_WAIT_MS", 20)
    scheduler = asyncio.create_task(clip_module._scheduler_loop())
    try:
        await asyncio.sleep(0)
        cancelled = clip_module._ImageBatchItem([1, 2])
        cancelled.future.cancel()
        await clip_module._image_queue.put(cancelled)
        assert await asyncio.wait_for(clip_module.embed_images_batched([3]), timeout=2) == [[3.0]]
        assert not scheduler.done()
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
