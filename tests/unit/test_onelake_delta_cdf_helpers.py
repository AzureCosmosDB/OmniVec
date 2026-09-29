from pathlib import Path
import sys
from datetime import datetime, timezone


sys.path.insert(
    0, str(Path(__file__).parents[2] / "connectors" / "fabric_spark")
)
from onelake_delta_cdf_helpers import build_messages, ordered_source_version  # noqa: E402


def _plan():
    return {
        "pipeline_id": "pipeline",
        "pipeline_name": "Documents",
        "docgrok_pipeline": "text-model",
        "source_id": "source",
        "destination_id": "destination",
        "destination_type": "onelake-iceberg",
        "destination_config": {"target_table": "dbo.documents"},
        "content_fields": ["title", "body"],
        "pipeline_generation": "2",
        "pipeline_revision": 3,
        "writeback_columns": {},
    }


def test_insert_builds_deterministic_upsert_message():
    row = {"id": "doc-1", "title": "Hello", "body": "World"}
    first = build_messages(row, "insert", 17, [_plan()])
    second = build_messages(row, "insert", 17, [_plan()])

    assert first == second
    assert first[0]["content"] == "Hello\n\nWorld"
    assert first[0]["source_version"] == 17
    assert first[0]["pipeline_revision"] == 3
    assert "message_type" not in first[0]


def test_omnivec_metadata_only_update_is_filtered():
    row = {
        "id": "doc-1",
        "title": "Hello",
        "body": "World",
        "content_hash": (
            "286346b7b2f097fc1c8d8c0436c5e3b1b661768a549f7585a3bda9cc7af2b079"
        ),
        "pipeline_id": "pipeline",
        "pipeline_generation": "2",
        "embedding_model": "text-model",
    }
    assert build_messages(row, "update_postimage", 18, [_plan()]) == []


def test_content_update_builds_new_upsert():
    row = {
        "id": "doc-1",
        "title": "Updated",
        "body": "World",
        "content_hash": "old",
        "pipeline_id": "pipeline",
        "pipeline_generation": "2",
        "embedding_model": "text-model",
    }
    messages = build_messages(row, "update_postimage", 19, [_plan()])
    assert len(messages) == 1
    assert messages[0]["content"] == "Updated\n\nWorld"


def test_delete_and_empty_content_build_delete_messages():
    deleted = build_messages({"id": "doc-1"}, "delete", 20, [_plan()])
    emptied = build_messages(
        {"id": "doc-1", "title": "", "body": None},
        "update_postimage",
        21,
        [_plan()],
    )

    assert deleted[0]["message_type"] == "delete"
    assert emptied[0]["message_type"] == "delete"
    assert deleted[0]["message_id"] != emptied[0]["message_id"]


def test_update_preimage_is_ignored():
    assert build_messages(
        {"id": "doc-1", "title": "Old"}, "update_preimage", 22, [_plan()]
    ) == []


def test_source_version_orders_by_commit_timestamp_then_version():
    earlier = ordered_source_version(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        999,
    )
    later = ordered_source_version(
        datetime(2026, 1, 1, 0, 0, 0, 1000, tzinfo=timezone.utc),
        0,
    )

    assert earlier < later
