from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).parents[2] / "connectors" / "ingestion" / "onelake_iceberg"))
from helpers import (  # noqa: E402
    checkpoint_key, content_from_row, content_hash, message_id,
    pipeline_fingerprint, row_has_current_omnivec_embedding,
)


def test_content_hash_uses_only_configured_user_fields():
    row = {
        "id": "7",
        "title": "Hello",
        "body": "World",
        "embedding": [0.2],
        "content_hash": "obsolete",
        "omnivec_writer": "omnivec-onelake-iceberg-v1",
    }
    content = content_from_row(row, ["title", "body", "embedding", "content_hash"])
    assert content == "Hello\n\nWorld"
    assert content_hash(content) == content_hash("Hello\n\nWorld")


def test_idempotency_identifies_pipeline_source_ref_and_content():
    first = message_id("p1", "s1", "r1", "a" * 64)
    assert first == message_id("p1", "s1", "r1", "a" * 64)
    assert first != message_id("p2", "s1", "r1", "a" * 64)
    assert checkpoint_key("p1", "revision", "r1") == "p1:revision:r1"


def test_current_writeback_is_skipped_but_changed_model_is_not():
    digest = content_hash("selected text")
    row = {
        "content_hash": digest,
        "embedding_model": "text-model",
        "pipeline_generation": "2",
        "pipeline_id": "pipeline",
    }
    assert row_has_current_omnivec_embedding(row, digest, "pipeline", "text-model", "2", {})
    assert not row_has_current_omnivec_embedding(row, digest, "pipeline", "other-model", "2", {})


def test_pipeline_fingerprint_changes_with_writeback_or_mirror_configuration():
    pipeline = {"id": "p1", "generation": "1", "docgrok_pipeline": "model"}
    destination = {
        "id": "d1",
        "config": {"writeback_columns": {"embedding_field": "embedding"}},
    }
    initial = pipeline_fingerprint(pipeline, destination)
    destination["config"]["mirror"] = {"type": "redis", "config": {"endpoint": "cache:10000"}}
    assert pipeline_fingerprint(pipeline, destination) != initial
