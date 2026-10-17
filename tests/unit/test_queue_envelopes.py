"""Queue packing, pacing and confirmed-send contracts without a live broker."""

import asyncio
import json
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.servicebus.exceptions import MessageSizeExceededError

from omnivec_queue import (
    MAX_ENVELOPE_BYTES, publish_queue_documents, queue_document_limit, queue_messages, queue_publish_policy,
)


def document(index, **changes):
    return {
        "message_id": f"doc-{index}", "pipeline_id": "pipeline",
        "destination_id": "destination", "docgrok_pipeline": "model",
        "content": "hello", **changes,
    }


def unpack(message):
    return message.get("documents", [message])


@pytest.mark.parametrize("count", [1, 10, 100, 1000])
def test_count_boundaries_stable_ids_and_legacy_tail(count):
    originals = [document(index) for index in range(201)]
    packed = list(queue_messages(originals, count))
    assert [item for message, _, _ in packed for item in unpack(message)] == originals
    assert all(size <= count for _, _, size in packed)
    assert sum(size for _, _, size in packed) == 201
    assert packed == list(queue_messages(originals, count))
    if count == 100:
        assert [size for _, _, size in packed] == [100, 100, 1]
    if count == 1:
        assert all("queue_envelope_version" not in message for message, _, _ in packed)


@pytest.mark.parametrize("invalid", [0, -1, 1001, True, "100", 1.5])
def test_document_limit_validation(invalid):
    with pytest.raises(ValueError):
        queue_document_limit({"queue_documents_per_message": invalid})
    assert queue_document_limit({}) == 1


@pytest.mark.parametrize("field,value", [
    ("pipeline_id", "other"), ("destination_id", "other"),
    ("docgrok_pipeline", "other"), ("pipeline_generation", "2"),
    ("source_id", "other"), ("message_type", "delete"),
    ("benchmark_mock_embedding", True), ("resource_weight", 20),
])
def test_incompatible_routes_and_repeated_ids_are_not_combined(field, value):
    originals = [document(0), document(1, **{field: value}), document(2)]
    assert len(list(queue_messages(originals, 100))) == 3
    assert len(list(queue_messages([document(0), document(0)], 100))) == 2


def test_serialized_utf8_byte_cap_and_large_legacy_documents():
    originals = [document(index, content="\u6771\u4eac" * 10_000) for index in range(10)]
    packed = list(queue_messages(originals, 100))
    assert len(packed) > 1
    assert all(len(body) <= MAX_ENVELOPE_BYTES for _, body, _ in packed)
    assert [item for message, _, _ in packed for item in unpack(message)] == originals
    large = document(0, content="x" * MAX_ENVELOPE_BYTES)
    assert list(queue_messages([large], 100))[0][0] == large


class Batch:
    def __init__(self, maximum):
        self.maximum = maximum
        self.messages = []
        self.size = 0

    def __len__(self):
        return len(self.messages)

    def add_message(self, message):
        size = len(b"".join(message.body)) + 512
        if self.size + size > self.maximum:
            raise MessageSizeExceededError()
        self.messages.append(message)
        self.size += size


class Sender:
    def __init__(self, fail_send=0):
        self.fail_send = fail_send
        self.sends = 0
        self.accepted = []
        self.batch_limits = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def create_message_batch(self, *, max_size_in_bytes):
        self.batch_limits.append(max_size_in_bytes)
        return Batch(max_size_in_bytes)

    async def send_messages(self, batch):
        self.sends += 1
        if self.sends == self.fail_send:
            raise RuntimeError("Broker send failed")
        self.accepted.extend(batch.messages)


def test_confirmed_publish_counts_documents_and_preserves_legacy_identity():
    sender = Sender()
    originals = [document(index) for index in range(205)]
    assert asyncio.run(publish_queue_documents(sender, originals, 100)) == 205
    assert len(sender.accepted) == 3
    assert sum(len(unpack(json.loads(b"".join(message.body)))) for message in sender.accepted) == 205
    assert all(message.subject == "pipeline" for message in sender.accepted)
    legacy = Sender()
    asyncio.run(publish_queue_documents(legacy, [document(0)], 1, preserve_legacy_identity=True))
    assert legacy.accepted[0].subject is None
    assert legacy.accepted[0].message_id != "doc-0"


def test_send_failure_and_oversized_document_are_not_success_shaped():
    sender = Sender(fail_send=2)
    with pytest.raises(RuntimeError, match="Broker send failed"):
        asyncio.run(publish_queue_documents(sender, [
            document(index, content="x" * 150_000) for index in range(3)
        ], 100))
    assert sender.accepted
    with pytest.raises(MessageSizeExceededError):
        asyncio.run(publish_queue_documents(Sender(), [
            document(0, content="x" * 300_000)
        ], 100))


def large_queue_policy():
    return {
        "queue_documents_per_message": 500,
        "queue_max_envelope_bytes": 600 * 1024,
        "queue_send_batch_max_bytes": 640 * 1024,
        "queue_publish_batch_size": 500,
    }


@pytest.mark.parametrize("field,value", [
    ("queue_max_envelope_bytes", 0), ("queue_max_envelope_bytes", 961 * 1024),
    ("queue_send_batch_max_bytes", 1025 * 1024),
    ("queue_send_batch_max_bytes", 600 * 1024),
    ("queue_publish_batch_size", 0), ("queue_publish_batch_size", 1001),
    ("queue_max_envelope_bytes", True), ("queue_publish_batch_size", "500"),
])
def test_live_byte_and_publish_limits_fail_closed(field, value):
    with pytest.raises(ValueError):
        queue_publish_policy({**large_queue_policy(), field: value})


def test_500_one_kib_documents_fit_one_configured_message():
    originals = []
    for index in range(500):
        item = document(index, content="")
        item["content"] = "x" * (1024 - len(json.dumps(item, separators=(",", ":")).encode()))
        originals.append(item)
    policy = queue_publish_policy(large_queue_policy())
    sender = Sender()
    assert asyncio.run(publish_queue_documents(sender, originals, policy)) == 500
    assert len(sender.accepted) == 1
    encoded = b"".join(sender.accepted[0].body)
    assert 500 * 1024 < len(encoded) < policy.max_envelope_bytes
    assert json.loads(encoded)["documents"] == originals
    assert sender.batch_limits == [640 * 1024]


def test_mixed_pipeline_byte_policies_keep_native_send_limits_separate():
    sender = Sender()
    policies = {"small": queue_publish_policy({}), "large": queue_publish_policy(large_queue_policy())}
    originals = [document(0, pipeline_id="small"), document(1, pipeline_id="large", content="x" * 300_000)]
    assert asyncio.run(publish_queue_documents(sender, originals, policies)) == 2
    assert sender.batch_limits == [240 * 1024, 640 * 1024]
    assert [message.subject for message in sender.accepted] == ["small", "large"]


def test_running_synthetic_publisher_refreshes_200_to_500_without_restart(api_app, monkeypatch):
    import synthetic_source

    clock = [0.0]
    sender = Sender()
    send = sender.send_messages
    async def send_and_tick(batch):
        await send(batch)
        clock[0] += 2
    sender.send_messages = send_and_tick
    credential = SimpleNamespace(close=lambda: None)
    async def close():
        pass
    credential.close = close
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def get_topic_sender(self, _):
            return sender
    async def refresh():
        return {**large_queue_policy(), "embed_batch_size": 512}
    progress = []
    async def report(count):
        progress.append(count)
    monkeypatch.setattr(synthetic_source, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(synthetic_source, "ServiceBusClient", lambda *_: Client())
    monkeypatch.setattr(synthetic_source.time, "monotonic", lambda: clock[0])
    asyncio.run(synthetic_source.publish_synthetic_messages(
        namespace="test", topic_name="embeddings", source={"id": "src"},
        pipeline={"id": "pip", "name": "Benchmark", "docgrok_pipeline": "mdl",
                  "resource_policy": {"queue_documents_per_message": 200}},
        destination={"id": "dst", "type": "null", "config": {"benchmark_only": True}},
        run_id="run", document_count=1600, message_size_bytes=1024,
        docs_per_second=0, content_pattern="deterministic", mock_embedding=True,
        mock_embedding_dimensions=384, progress=report, refresh_resource_policy=refresh,
    ))
    received = [json.loads(b"".join(message.body)) for message in sender.accepted]
    assert [len(unpack(message)) for message in received] == [200, 200, 200, 500, 500]
    assert sender.batch_limits == [240 * 1024] * 3 + [640 * 1024] * 2
    docs = [doc for message in received for doc in unpack(message)]
    assert len({doc["message_id"] for doc in docs}) == len(docs) == 1600
    assert all(doc["embed_batch_size"] == 50 for doc in docs[:600])
    assert all(doc["embed_batch_size"] == doc["model_max_batch_size"] == 512 for doc in docs[600:])
    assert progress[-1] == 1600


@pytest.mark.parametrize("rate,expected_broker_count", [(0, 3), (10, 205)])
def test_synthetic_packing_document_progress_and_paced_partial_flush(api_app, monkeypatch, rate, expected_broker_count):
    import synthetic_source

    sender = Sender()
    credential = SimpleNamespace(closed=False)
    clock = [0.0]
    async def close():
        credential.closed = True
    credential.close = close
    async def sleep(delay):
        clock[0] += delay
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def get_topic_sender(self, _):
            return sender
    monkeypatch.setattr(synthetic_source, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(synthetic_source, "ServiceBusClient", lambda *_: Client())
    monkeypatch.setattr(synthetic_source.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(synthetic_source.asyncio, "sleep", sleep)
    progress = []
    async def report(count):
        progress.append(count)
    asyncio.run(synthetic_source.publish_synthetic_messages(
        namespace="test.servicebus.windows.net", topic_name="embeddings",
        source={"id": "src"}, pipeline={
            "id": "pip", "name": "Benchmark", "docgrok_pipeline": "mdl",
            "resource_policy": {"queue_documents_per_message": 100},
        },
        destination={"id": "dst", "type": "null", "config": {"benchmark_only": True}},
        run_id="run", document_count=205, message_size_bytes=1024,
        docs_per_second=rate, content_pattern="deterministic",
        mock_embedding=True, mock_embedding_dimensions=384, progress=report,
    ))
    assert credential.closed
    assert progress[-1] == 205 and progress == sorted(progress)
    assert len(sender.accepted) == expected_broker_count
    documents = [item for message in sender.accepted for item in unpack(json.loads(b"".join(message.body)))]
    assert len(documents) == len({item["message_id"] for item in documents}) == 205
    if rate:
        assert clock[0] == pytest.approx(20.4)


def test_fabric_cdf_packs_each_pipeline_and_surfaces_microbatch_send_failure(monkeypatch):
    directory = Path(__file__).parents[2] / "connectors" / "fabric_spark"
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.setitem(sys.modules, "pyspark", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "pyspark.sql", SimpleNamespace(
        DataFrame=object, SparkSession=object, functions=SimpleNamespace(),
    ))
    spec = importlib.util.spec_from_file_location("cdf_queue_test", directory / "onelake_delta_cdf_stream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plans = [{
        "pipeline_id": pipeline, "pipeline_name": pipeline,
        "source_id": "source", "docgrok_pipeline": "model",
        "destination_id": "dest", "destination_type": "null", "destination_config": {},
        "resource_policy": {"queue_documents_per_message": 2},
    } for pipeline in ("A", "B")]
    rows = [SimpleNamespace(asDict=lambda recursive, index=index: {
        "id": str(index), "content": "hello", "_change_type": "insert",
        "_commit_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc), "_commit_version": 1,
    }) for index in range(5)]
    batch = SimpleNamespace(orderBy=lambda *_: SimpleNamespace(collect=lambda: rows))
    accepted = []
    monkeypatch.setattr(module, "_create_sas_token", lambda *_: "test-token")
    monkeypatch.setattr(module, "_send_message", lambda _endpoint, _auth, message: accepted.append(message))
    module._publish_microbatch(batch, 1, plans, "https://local.invalid/messages", "sas", {
        "SharedAccessKeyName": "test", "SharedAccessKey": "test",
    })
    assert len(accepted) == 6
    assert sorted(len(unpack(message)) for message in accepted) == [1, 1, 2, 2, 2, 2]
    assert sum(len(unpack(message)) for message in accepted) == 10
    def fail(*_):
        raise RuntimeError("Send failure must abort the Spark microbatch")
    monkeypatch.setattr(module, "_send_message", fail)
    with pytest.raises(RuntimeError, match="abort the Spark microbatch"):
        module._publish_microbatch(batch, 2, plans, "https://local.invalid/messages", "sas", {
            "SharedAccessKeyName": "test", "SharedAccessKey": "test",
        })
