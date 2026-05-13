"""Validation tests for the Pydantic models in ``api/models.py``.

Two complementary patterns:

* Round-trip happy path: construct → ``.model_dump()`` → reconstruct → equal.
* Negative tests: missing required field, wrong type, invalid enum value.

We do *not* use ``hypothesis.from_type`` for the FastAPI models because
several fields are ``Dict[str, Any]`` (open-ended) which hypothesis cannot
shrink usefully — explicit parametrized cases catch the regressions we
care about with much higher signal.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from hypothesis import given, strategies as st


# ===========================================================================
# Enums — exhaustive value assertions catch silent renames.
# ===========================================================================
class TestEnums:
    def test_source_type_values(self, api_models):
        assert {e.value for e in api_models.SourceType} == {
            "azure-blob", "cosmosdb", "postgresql", "mssql", "s3", "http"
        }

    def test_destination_type_values(self, api_models):
        assert {e.value for e in api_models.DestinationType} == {
            "cosmosdb-vector", "pgvector", "mssql"
        }

    def test_job_status_values(self, api_models):
        assert {e.value for e in api_models.JobStatus} == {
            "pending", "processing", "completed", "failed", "cancelled"
        }

    def test_pipeline_status_values(self, api_models):
        assert {e.value for e in api_models.PipelineStatus} == {
            "active", "paused", "error"
        }

    def test_trigger_type_values(self, api_models):
        assert {e.value for e in api_models.TriggerType} == {
            "event-grid", "change-feed", "schedule", "manual"
        }

    def test_model_category_values(self, api_models):
        assert {e.value for e in api_models.ModelCategory} == {"embedding", "chat"}


# ===========================================================================
# Source round-trip + validation
# ===========================================================================
class TestSource:
    def test_round_trip(self, api_models):
        s = api_models.Source(name="src1", type=api_models.SourceType.AZURE_BLOB, config={"container": "c"})
        s2 = api_models.Source(**s.model_dump())
        assert s == s2

    def test_missing_name(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Source(type=api_models.SourceType.AZURE_BLOB, config={})

    def test_missing_type(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Source(name="x", config={})

    def test_invalid_source_type(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Source(name="x", type="not-a-type", config={})

    def test_invalid_config_type(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Source(name="x", type=api_models.SourceType.HTTP, config="not-a-dict")

    @given(st.text(min_size=1, max_size=40))
    def test_name_text_round_trip(self, api_models, name):
        s = api_models.Source(name=name, type=api_models.SourceType.HTTP, config={})
        assert s.name == name


# ===========================================================================
# Destination
# ===========================================================================
class TestDestination:
    def test_round_trip(self, api_models):
        d = api_models.Destination(name="d", type=api_models.DestinationType.PGVECTOR, config={"host": "h"})
        d2 = api_models.Destination(**d.model_dump())
        assert d == d2

    def test_invalid_type(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Destination(name="d", type="bogus", config={})

    def test_missing_required(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Destination(type=api_models.DestinationType.PGVECTOR, config={})


# ===========================================================================
# Pipeline
# ===========================================================================
class TestPipeline:
    def _mk_source(self, api_models):
        return api_models.PipelineSource(source_id="s1")

    def test_round_trip(self, api_models):
        p = api_models.Pipeline(
            name="p", sources=[self._mk_source(api_models)],
            docgrok_pipeline="default", destination_id="d1",
            vector_index_path="/embedding/*",
        )
        p2 = api_models.Pipeline(**p.model_dump())
        assert p == p2

    def test_missing_destination_id(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Pipeline(
                name="p", sources=[self._mk_source(api_models)],
                docgrok_pipeline="default",
                vector_index_path="/embedding/*",
            )

    def test_invalid_status(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Pipeline(
                name="p", sources=[self._mk_source(api_models)],
                docgrok_pipeline="default", destination_id="d1",
                vector_index_path="/x", status="bogus",
            )

    def test_sources_must_be_list(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Pipeline(
                name="p", sources="not-a-list",
                docgrok_pipeline="default", destination_id="d1",
                vector_index_path="/x",
            )

    def test_default_status_is_active(self, api_models):
        p = api_models.Pipeline(
            name="p", sources=[self._mk_source(api_models)],
            docgrok_pipeline="default", destination_id="d1",
            vector_index_path="/x",
        )
        assert p.status == api_models.PipelineStatus.ACTIVE


# ===========================================================================
# Job + JobStats
# ===========================================================================
class TestJob:
    def test_round_trip(self, api_models):
        j = api_models.Job(pipeline_id="p", source_id="s", source_ref="blob://x")
        j2 = api_models.Job(**j.model_dump())
        assert j == j2

    def test_default_status_pending(self, api_models):
        j = api_models.Job(pipeline_id="p", source_id="s", source_ref="x")
        assert j.status == api_models.JobStatus.PENDING

    def test_invalid_status(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Job(pipeline_id="p", source_id="s", source_ref="x", status="bogus")

    def test_missing_pipeline_id(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Job(source_id="s", source_ref="x")

    def test_retry_count_must_be_int(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Job(pipeline_id="p", source_id="s", source_ref="x", retry_count="not-int")


class TestJobStats:
    def test_defaults(self, api_models):
        s = api_models.JobStats()
        assert s.total == 0 and s.failed == 0

    @given(
        st.integers(min_value=0, max_value=1_000_000),
        st.integers(min_value=0, max_value=1_000_000),
    )
    def test_round_trip(self, api_models, total, failed):
        s = api_models.JobStats(total=total, failed=failed)
        s2 = api_models.JobStats(**s.model_dump())
        assert s == s2


# ===========================================================================
# Request models
# ===========================================================================
class TestCreateSourceRequest:
    def test_round_trip(self, api_models):
        r = api_models.CreateSourceRequest(name="x", type=api_models.SourceType.S3, config={})
        r2 = api_models.CreateSourceRequest(**r.model_dump())
        assert r == r2

    def test_missing_required(self, api_models):
        with pytest.raises(ValidationError):
            api_models.CreateSourceRequest(type=api_models.SourceType.S3, config={})


class TestCreateDestinationRequest:
    def test_round_trip(self, api_models):
        r = api_models.CreateDestinationRequest(
            name="x", type=api_models.DestinationType.COSMOSDB_VECTOR, config={}
        )
        assert api_models.CreateDestinationRequest(**r.model_dump()) == r

    def test_invalid_enum(self, api_models):
        with pytest.raises(ValidationError):
            api_models.CreateDestinationRequest(name="x", type="bogus", config={})


class TestCreatePipelineRequest:
    def test_round_trip(self, api_models):
        r = api_models.CreatePipelineRequest(
            name="p",
            sources=[api_models.PipelineSource(source_id="s1")],
            docgrok_pipeline="default", destination_id="d1",
            vector_index_path="/v",
        )
        assert api_models.CreatePipelineRequest(**r.model_dump()) == r

    def test_missing_required(self, api_models):
        with pytest.raises(ValidationError):
            api_models.CreatePipelineRequest(
                name="p", sources=[],
                docgrok_pipeline="default", destination_id="d1",
            )


# ===========================================================================
# Assistant
# ===========================================================================
class TestAssistant:
    def test_round_trip(self, api_models):
        a = api_models.Assistant(name="a", model_id="m")
        assert api_models.Assistant(**a.model_dump()) == a

    def test_default_top_k_is_5(self, api_models):
        a = api_models.Assistant(name="a", model_id="m")
        assert a.top_k == 5

    def test_temperature_must_be_numeric(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Assistant(name="a", model_id="m", temperature="warm")

    def test_missing_model_id(self, api_models):
        with pytest.raises(ValidationError):
            api_models.Assistant(name="a")


class TestAssistantChatRequest:
    def test_round_trip(self, api_models):
        r = api_models.AssistantChatRequest(message="hi")
        assert api_models.AssistantChatRequest(**r.model_dump()) == r

    def test_missing_message(self, api_models):
        with pytest.raises(ValidationError):
            api_models.AssistantChatRequest()


# ===========================================================================
# Sub-configs
# ===========================================================================
class TestChunkConfig:
    def test_defaults(self, api_models):
        c = api_models.ChunkConfig()
        assert c.chunk_size == 1000
        assert c.chunk_overlap == 200

    @given(
        st.integers(min_value=1, max_value=100_000),
        st.integers(min_value=0, max_value=10_000),
    )
    def test_round_trip(self, api_models, size, overlap):
        c = api_models.ChunkConfig(chunk_size=size, chunk_overlap=overlap)
        assert api_models.ChunkConfig(**c.model_dump()) == c


class TestPgVectorConfig:
    def test_round_trip(self, api_models):
        c = api_models.PgVectorConfig(host="h", database="d", table="t")
        assert api_models.PgVectorConfig(**c.model_dump()) == c

    def test_missing_required(self, api_models):
        with pytest.raises(ValidationError):
            api_models.PgVectorConfig(host="h", database="d")  # missing table


class TestHTTPConfig:
    def test_round_trip(self, api_models):
        c = api_models.HTTPConfig(url="https://example.com")
        assert api_models.HTTPConfig(**c.model_dump()) == c

    def test_url_required(self, api_models):
        with pytest.raises(ValidationError):
            api_models.HTTPConfig(method="GET")
