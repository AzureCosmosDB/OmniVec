from pathlib import Path
import io
import sys

import pyarrow as pa
import pyarrow.parquet as pq


sys.path.insert(0, str(Path(__file__).parents[2] / "connectors" / "ingestion" / "onelake_iceberg"))
from helpers import (  # noqa: E402
    checkpoint_json, checkpoint_key, content_from_row, content_hash, message_id,
    ordered_source_version, pipeline_fingerprint, read_projected_rows,
    row_has_current_omnivec_embedding,
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
    assert first != message_id("p1", "s1", "r1", "a" * 64, 2)
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


def test_checkpoint_persists_known_refs_for_delete_detection():
    payload = checkpoint_json(
        "snapshot",
        {},
        {"pipeline": "fingerprint"},
        {},
        {"pipeline": ["row-1", "row-2"]},
        {"pipeline": 3},
        {"pipeline": ["row-3"]},
    )
    assert b'"known_refs":{"pipeline":["row-1","row-2"]}' in payload
    assert b'"pipeline_revisions":{"pipeline":3}' in payload
    assert b'"pending_empty_refs":{"pipeline":["row-3"]}' in payload


def test_source_version_orders_by_snapshot_timestamp_then_sequence():
    assert ordered_source_version(1000, 999) < ordered_source_version(1001, 0)


def test_scan_falls_back_when_delta_cdf_column_is_missing_from_name_mapping():
    sink = io.BytesIO()
    pq.write_table(
        pa.table(
            {
                "id": ["row-1"],
                "title": ["Title"],
                "_change_type": ["update_postimage"],
            }
        ),
        sink,
    )
    payload = sink.getvalue()

    class Scan:
        def to_arrow(self):
            raise ValueError("Could not find field with name: _change_type")

        def plan_files(self):
            file = type("File", (), {"file_path": "row.parquet"})()
            return [type("Task", (), {"delete_files": [], "file": file})()]

    class Input:
        def open(self):
            return io.BytesIO(payload)

    class Table:
        io = type("IO", (), {"new_input": lambda self, path: Input()})()

        def scan(self, selected_fields):
            return Scan()

    rows, used_fallback = read_projected_rows(Table(), ("id", "title", "content_hash"))

    assert used_fallback
    assert rows == [{"id": "row-1", "title": "Title", "content_hash": None}]
