"""Unit tests for Cosmos full-text search mode (search/searcher.py).

Covers schema validation, tokenization, SQL shape, per-index branching,
and RRF merging between fts + vector indexes.
"""
from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Module loader — reuse the same sys.path trick the search_app fixture uses
# but without instantiating the FastAPI app (we only need searcher + schemas).
# ---------------------------------------------------------------------------
@pytest.fixture
def search_mods(monkeypatch):
    import pathlib
    REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
    SEARCH_DIR = REPO_ROOT / "search"
    for n in ["main", "auth", "schemas", "searcher"]:
        sys.modules.pop(n, None)
    monkeypatch.syspath_prepend(str(SEARCH_DIR))
    schemas = importlib.import_module("schemas")
    searcher = importlib.import_module("searcher")
    return schemas, searcher


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def test_vector_mode_default_requires_embedding(search_mods):
    schemas, _ = search_mods
    with pytest.raises(Exception):  # ValidationError
        schemas.IndexSpec(
            id="i1",
            store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
        )


def test_fts_mode_does_not_require_embedding(search_mods):
    schemas, _ = search_mods
    idx = schemas.IndexSpec(
        id="i1",
        mode="fts",
        store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
    )
    assert idx.embedding is None
    assert idx.fts_field is None  # defaults to first content_field at search time


def test_fts_mode_rejects_pgvector_store(search_mods):
    schemas, _ = search_mods
    with pytest.raises(Exception):
        schemas.IndexSpec(
            id="i1",
            mode="fts",
            store=schemas.PgVectorStore(dsn="postgres://x", table="t"),
        )


def test_fts_mode_rejects_invalid_field_path(search_mods):
    schemas, _ = search_mods
    with pytest.raises(Exception):
        schemas.IndexSpec(
            id="i1",
            mode="fts",
            fts_field="c.content; DROP TABLE",
            store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
        )


def test_fts_mode_accepts_nested_path(search_mods):
    schemas, _ = search_mods
    idx = schemas.IndexSpec(
        id="i1",
        mode="fts",
        fts_field="body.text",
        store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
    )
    assert idx.fts_field == "body.text"


def test_fts_mode_requires_field_when_content_fields_empty(search_mods):
    schemas, _ = search_mods
    with pytest.raises(Exception):
        schemas.IndexSpec(
            id="i1",
            mode="fts",
            content_fields=[],
            store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
        )


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
def test_tokenize_dedupes_and_lowercases(search_mods):
    _, searcher = search_mods
    assert searcher._tokenize_fts_query("Hello hello WORLD world") == ["hello", "world"]


def test_tokenize_handles_punctuation(search_mods):
    _, searcher = search_mods
    toks = searcher._tokenize_fts_query("don't break, it's fine.")
    assert "don't" in toks and "it's" in toks and "fine" in toks


def test_tokenize_caps_terms(search_mods, monkeypatch):
    _, searcher = search_mods
    monkeypatch.setattr(searcher, "_FTS_MAX_TERMS", 3)
    q = "alpha beta gamma delta epsilon"
    assert searcher._tokenize_fts_query(q) == ["alpha", "beta", "gamma"]


def test_tokenize_empty(search_mods):
    _, searcher = search_mods
    assert searcher._tokenize_fts_query("") == []
    assert searcher._tokenize_fts_query("   ,, ") == []


# ---------------------------------------------------------------------------
# search_cosmos_fts — verify SQL shape via a fake container.
# ---------------------------------------------------------------------------
class _FakeContainer:
    def __init__(self, docs):
        self._docs = docs
        self.last_query = None
        self.last_params = None

    def query_items(self, query, parameters, enable_cross_partition_query):
        self.last_query = query
        self.last_params = parameters
        return [{"c": d} for d in self._docs]


class _FakeDatabase:
    def __init__(self, container):
        self._container = container

    def get_container_client(self, _):
        return self._container


class _FakeClient:
    def __init__(self, container):
        self._db = _FakeDatabase(container)

    def get_database_client(self, _):
        return self._db


def test_search_cosmos_fts_builds_expected_sql(search_mods, monkeypatch):
    schemas, searcher = search_mods
    container = _FakeContainer([
        {"id": "a", "content": "hello world", "source": "s1"},
        {"id": "b", "content": "world peace", "source": "s2"},
    ])
    monkeypatch.setattr(searcher, "CosmosClient", None, raising=False)

    # Patch the local import inside search_cosmos_fts.
    import azure.cosmos
    monkeypatch.setattr(
        azure.cosmos, "CosmosClient", lambda *a, **kw: _FakeClient(container)
    )
    import azure.identity
    monkeypatch.setattr(
        azure.identity, "DefaultAzureCredential", lambda *a, **kw: object()
    )

    store = schemas.CosmosStore(endpoint="https://x", database="d", container="c")
    hits = asyncio.run(searcher.search_cosmos_fts(
        store=store,
        fts_field="content",
        query_terms=["hello", "world"],
        top_k=5,
        content_fields=["content"],
        return_fields=[],
        index_filter=None,
        include_vector=False,
    ))

    assert len(hits) == 2
    assert hits[0]["score"] is None  # FullTextScore not projectable
    assert hits[0]["text"] == "hello world"
    sql = container.last_query
    assert "ORDER BY RANK FullTextScore(c.content, @term0, @term1)" in sql
    assert "SELECT TOP @top_k c FROM c " in sql
    params = {p["name"]: p["value"] for p in container.last_params}
    assert params == {"@top_k": 5, "@term0": "hello", "@term1": "world"}


def test_search_cosmos_fts_rejects_invalid_field(search_mods):
    schemas, searcher = search_mods
    store = schemas.CosmosStore(endpoint="https://x", database="d", container="c")
    with pytest.raises(ValueError):
        asyncio.run(searcher.search_cosmos_fts(
            store=store, fts_field="c.x; DROP", query_terms=["a"], top_k=5,
            content_fields=["content"], return_fields=[],
            index_filter=None, include_vector=False,
        ))


def test_search_cosmos_fts_empty_terms_returns_empty(search_mods):
    schemas, searcher = search_mods
    store = schemas.CosmosStore(endpoint="https://x", database="d", container="c")
    hits = asyncio.run(searcher.search_cosmos_fts(
        store=store, fts_field="content", query_terms=[], top_k=5,
        content_fields=["content"], return_fields=[],
        index_filter=None, include_vector=False,
    ))
    assert hits == []


# ---------------------------------------------------------------------------
# _search_one_index branching
# ---------------------------------------------------------------------------
def test_search_one_index_fts_skips_embed(search_mods, monkeypatch):
    schemas, searcher = search_mods

    called_embed = {"n": 0}

    async def _fake_embed(*a, **kw):
        called_embed["n"] += 1
        raise AssertionError("embed_query must not be called for fts mode")

    monkeypatch.setattr(searcher, "embed_query", _fake_embed)

    async def _fake_fts(*a, **kw):
        return [{"id": "x", "score": None, "text": "hi", "metadata": {},
                 "source": None, "source_ref": None, "text_parts": None}]

    monkeypatch.setattr(searcher, "search_cosmos_fts", _fake_fts)

    idx = schemas.IndexSpec(
        id="i1", mode="fts",
        store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
    )
    info, hits = asyncio.run(searcher._search_one_index(
        http=None, idx=idx, default_per_index_top_k=10,
        query="hello world", request_id="rid", include_vector=False,
    ))
    assert called_embed["n"] == 0
    assert info.error is None
    assert info.embedding_model == "fts"
    assert info.result_count == 1
    assert hits[0]["id"] == "x"


def test_search_one_index_fts_errors_on_empty_query(search_mods):
    schemas, searcher = search_mods
    idx = schemas.IndexSpec(
        id="i1", mode="fts",
        store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
    )
    info, hits = asyncio.run(searcher._search_one_index(
        http=None, idx=idx, default_per_index_top_k=10,
        query="", request_id="rid", include_vector=False,
    ))
    assert hits == []
    assert "requires text query" in (info.error or "")


def test_search_one_index_fts_errors_on_pure_punctuation(search_mods):
    schemas, searcher = search_mods
    idx = schemas.IndexSpec(
        id="i1", mode="fts",
        store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
    )
    info, hits = asyncio.run(searcher._search_one_index(
        http=None, idx=idx, default_per_index_top_k=10,
        query=",,, !!", request_id="rid", include_vector=False,
    ))
    assert hits == []
    assert "no searchable tokens" in (info.error or "")


# ---------------------------------------------------------------------------
# explain_search handles FTS without embedding
# ---------------------------------------------------------------------------
def test_explain_search_with_fts_index(search_mods):
    schemas, searcher = search_mods
    req = schemas.SearchRequest(
        query="hello world",
        indexes=[schemas.IndexSpec(
            id="fts1", mode="fts",
            store=schemas.CosmosStore(endpoint="https://x", database="d", container="c"),
        )],
    )
    plan = asyncio.run(searcher.explain_search(req))
    assert len(plan["indexes"]) == 1
    p = plan["indexes"][0]
    assert p["mode"] == "fts"
    assert p["embedding"] is None
    assert p["fts_field"] == "content"
    assert p["fts_terms"] == ["hello", "world"]
