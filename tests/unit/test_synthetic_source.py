"""Synthetic source message construction contracts."""

import json

import pytest


def resources():
    source = {
        "id": "src-synthetic",
        "type": "synthetic",
        "config": {"benchmark_only": True},
    }
    pipeline = {
        "id": "pip-synthetic",
        "name": "Synthetic benchmark",
        "docgrok_pipeline": "mdl-embedding",
        "generation": "generation-1",
        "destination_id": "dst-null",
        "resource_policy": {
            "weight": 10,
            "max_concurrency_per_worker": 4,
            "embed_batch_size": 50,
            "priority": "normal",
            "workload_class": "shared",
        },
        "model_limits": {
            "max_batch_size": 256,
            "context_window_tokens": 8191,
            "max_batch_tokens": 6000,
        },
    }
    destination = {
        "id": "dst-null",
        "type": "null",
        "config": {
            "benchmark_only": True,
            "max_docs_per_second": 0,
            "rate_key": "synthetic",
        },
    }
    return source, pipeline, destination


@pytest.mark.parametrize("size", [1024, 2048, 5000, 10_000, 100_000])
def test_synthetic_message_has_exact_serialized_size(api_app, size):
    from synthetic_source import build_synthetic_message

    source, pipeline, destination = resources()
    message, body = build_synthetic_message(
        source=source,
        pipeline=pipeline,
        destination=destination,
        run_id="syn-contract",
        index=7,
        message_size_bytes=size,
        content_pattern="deterministic",
        mock_embedding=True,
        mock_embedding_dimensions=768,
    )

    assert len(body) == size
    assert json.loads(body) == message
    assert message["source_ref"] == "syn-contract-0000000007"
    assert len(message["message_id"]) == 64
    assert len(message["content_hash"]) == 64
    assert message["benchmark_mock_embedding"] is True
    assert message["benchmark_mock_embedding_dimensions"] == 768


def test_synthetic_message_is_deterministic(api_app):
    from synthetic_source import build_synthetic_message

    source, pipeline, destination = resources()
    kwargs = {
        "source": source,
        "pipeline": pipeline,
        "destination": destination,
        "run_id": "syn-repeat",
        "index": 3,
        "message_size_bytes": 5000,
        "content_pattern": "deterministic",
    }
    assert build_synthetic_message(**kwargs) == build_synthetic_message(**kwargs)


def test_default_mock_dimensions_preserve_one_kilobyte_messages(api_app):
    from synthetic_source import build_synthetic_message

    source, pipeline, destination = resources()
    message, body = build_synthetic_message(
        source=source,
        pipeline=pipeline,
        destination=destination,
        run_id="syn-mock",
        index=0,
        message_size_bytes=1024,
        content_pattern="deterministic",
        mock_embedding=True,
        mock_embedding_dimensions=384,
    )

    assert len(body) == 1024
    assert message["benchmark_mock_embedding"] is True
    assert "benchmark_mock_embedding_dimensions" not in message


def test_synthetic_message_rejects_size_below_envelope(api_app):
    from synthetic_source import build_synthetic_message

    source, pipeline, destination = resources()
    with pytest.raises(ValueError, match="must be at least"):
        build_synthetic_message(
            source=source,
            pipeline=pipeline,
            destination=destination,
            run_id="syn-small",
            index=0,
            message_size_bytes=100,
            content_pattern="repeated",
        )


@pytest.mark.parametrize("mock", [False, True])
def test_pipeline_batch_512_is_mock_only_and_preserves_real_model_limits(api_app, mock):
    from synthetic_source import build_synthetic_message

    source, pipeline, destination = resources()
    original = dict(pipeline["model_limits"])
    pipeline["resource_policy"]["embed_batch_size"] = 512
    message, body = build_synthetic_message(
        source=source, pipeline=pipeline, destination=destination,
        run_id="syn-batch", index=0, message_size_bytes=2048,
        content_pattern="deterministic", mock_embedding=mock,
    )
    assert len(body) == 2048
    assert message["embed_batch_size"] == 512
    assert message["model_max_batch_size"] == (512 if mock else 256)
    assert message["model_max_batch_tokens"] == original["max_batch_tokens"]
    assert message["model_context_window_tokens"] == original["context_window_tokens"]
    assert pipeline["model_limits"] == original
