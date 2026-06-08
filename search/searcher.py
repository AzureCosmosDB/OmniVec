"""Core vector-search logic for the OmniVec Search service.

Reusable across CosmosDB + pgvector stores. No FastAPI dependencies so this
module is also importable from tests.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from schemas import (
    CosmosStore,
    EmbeddingPolicy,
    IndexFilter,
    IndexSpec,
    MergeConfig,
    ModelEmbedding,
    PerIndexInfo,
    PgVectorStore,
    PipelineEmbedding,
    PrecomputedEmbedding,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchTiming,
)

logger = logging.getLogger(__name__)

DOCGROK_URL = os.getenv("DOCGROK_URL", "http://docgrok:80").rstrip("/")
PER_INDEX_TIMEOUT_S = float(os.getenv("SEARCH_PER_INDEX_TIMEOUT_S", "5"))
TOTAL_TIMEOUT_S = float(os.getenv("SEARCH_TOTAL_TIMEOUT_S", "15"))
EMBED_TIMEOUT_S = float(os.getenv("SEARCH_EMBED_TIMEOUT_S", "10"))


# =============================================================================
# Secret resolution (Key Vault)
# =============================================================================


_KV_RE = re.compile(r"^kv://([^/]+)/(.+)$")
_kv_cache: Dict[str, str] = {}


async def resolve_secret_ref(ref: str) -> str:
    """Resolve a `kv://<vault>/<secret>` reference to its plaintext value.

    Returns the ref unchanged if it isn't a Key Vault URI. Results are cached
    in-process for the pod lifetime.
    """
    if not ref or not ref.startswith("kv://"):
        return ref
    if ref in _kv_cache:
        return _kv_cache[ref]
    m = _KV_RE.match(ref)
    if not m:
        raise ValueError(f"Invalid secret ref format: {ref!r}")
    vault, secret = m.group(1), m.group(2)
    vault_url = f"https://{vault}.vault.azure.net" if "." not in vault else f"https://{vault}"

    def _get():
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
        return client.get_secret(secret).value

    value = await asyncio.to_thread(_get)
    _kv_cache[ref] = value
    return value


# =============================================================================
# Embedding — DocGrok router
# =============================================================================


async def embed_query(
    http: httpx.AsyncClient,
    policy: EmbeddingPolicy,
    query: str,
    request_id: str,
    query_image_b64: Optional[str] = None,
) -> Tuple[List[float], str]:
    """Return (vector, model_label) for an embedding policy."""
    if isinstance(policy, PrecomputedEmbedding):
        return list(policy.vector), "precomputed"

    modality = getattr(policy, "input_modality", "text")
    is_image = modality == "image"

    if is_image:
        # Decide payload: image bytes preferred, else fall back to the
        # text query (CLIP is multi-modal — text and image vectors share
        # the same space, so a text query against an image/video index
        # is valid via the CLIP text encoder).
        if isinstance(policy, ModelEmbedding):
            label = policy.model_id
            transform_name = "image-transform"
        elif isinstance(policy, PipelineEmbedding):
            label = policy.pipeline
            # For text queries we always embed via image-transform (the
            # CLIP text/image encoder pair). The pipeline used at
            # ingestion (e.g. video-transform = extract_frames →
            # image_embed) lives in the same 768-dim CLIP vector space,
            # so search-time embedding via image-transform is valid and
            # avoids running stages like extract_frames on text input.
            transform_name = policy.pipeline if query_image_b64 else "image-transform"
        else:
            raise ValueError(f"Unknown embedding policy: {policy!r}")
        if query_image_b64:
            body = {
                "data": query_image_b64,
                "transform_name": transform_name,
                "requestId": request_id,
            }
        elif query:
            body = {
                "text": query,
                "transform_name": transform_name,
                "requestId": request_id,
            }
        else:
            raise RuntimeError("image-modality index requires either text query or query_image_b64")
        resp = await http.post(f"{DOCGROK_URL}/embed", json=body, timeout=EMBED_TIMEOUT_S)
        if resp.status_code != 200:
            raise RuntimeError(
                f"embedding service returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        chunks = data.get("chunks") or []
        vec = (chunks[0].get("embedding") if chunks else None) or []
        if not vec:
            # Legacy text shape fallback (defensive).
            pages = data.get("pages") or data.get("output") or [[]]
            vec = pages[0] if pages else []
        if not vec:
            raise RuntimeError("image embedding service returned empty vector")
        if getattr(policy, "normalize", False):
            n = math.sqrt(sum(x * x for x in vec)) or 1.0
            vec = [x / n for x in vec]
        return list(vec), label

    # Text index, image-only query → skip with informative error.
    if not query:
        raise RuntimeError("text-modality index requires a text query (set query)")

    if isinstance(policy, ModelEmbedding):
        body = {"model_id": policy.model_id, "text": query, "requestId": request_id}
        label = policy.model_id
    elif isinstance(policy, PipelineEmbedding):
        body = {"pipeline": policy.pipeline, "text": query, "requestId": request_id}
        label = policy.pipeline
    else:
        raise ValueError(f"Unknown embedding policy: {policy!r}")

    resp = await http.post(f"{DOCGROK_URL}/embed", json=body, timeout=EMBED_TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(
            f"embedding service returned {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    pages = data.get("pages") or data.get("output") or [[]]
    vec = pages[0] if pages else []
    if not vec:
        raise RuntimeError("embedding service returned empty vector")

    if getattr(policy, "normalize", False):
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        vec = [x / n for x in vec]
    return list(vec), label


# =============================================================================
# Content-field helpers
# =============================================================================


def _extract_text(
    doc: dict, fields: List[str]
) -> Tuple[str, Optional[List[Dict[str, str]]]]:
    parts: List[Dict[str, str]] = []
    for f in fields:
        v = doc.get(f)
        if isinstance(v, str) and v:
            parts.append({"field": f, "value": v})
    if parts:
        text = "\n\n".join(p["value"] for p in parts)
        return text, (parts if len(parts) > 1 else None)
    fallback = doc.get("text") or doc.get("content") or ""
    return fallback, None


# =============================================================================
# Per-store search
# =============================================================================


async def search_cosmos(
    store: CosmosStore,
    vec_field: str,
    embedding: List[float],
    top_k: int,
    content_fields: List[str],
    return_fields: List[str],
    index_filter: Optional[IndexFilter],
    include_vector: bool,
) -> List[dict]:
    """Cosmos DB vector search (sync SDK wrapped with to_thread)."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    if getattr(store.auth, "mode", "managed_identity") == "key":
        key = await resolve_secret_ref(store.auth.secret_ref)  # type: ignore[attr-defined]
        client_args = {"credential": key}
    else:
        client_args = {"credential": DefaultAzureCredential()}

    def _run():
        client = CosmosClient(store.endpoint, **client_args)
        database = client.get_database_client(store.database)
        container = database.get_container_client(store.container)

        vf = vec_field.lstrip("/").replace("/", ".")
        # VectorDistance does not accept the vector as a bound param; inline it
        embedding_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"

        params: List[dict] = [{"name": "@top_k", "value": int(top_k)}]
        where = ""
        if index_filter and index_filter.where:
            where = f"WHERE {index_filter.where}"
            if isinstance(index_filter.params, dict):
                for k, v in index_filter.params.items():
                    name = k if k.startswith("@") else f"@{k}"
                    params.append({"name": name, "value": v})

        query = (
            f"SELECT TOP @top_k c, VectorDistance(c.{vf}, {embedding_str}) AS similarity "
            f"FROM c {where} ORDER BY VectorDistance(c.{vf}, {embedding_str})"
        )

        hits: List[dict] = []
        for item in container.query_items(
            query=query, parameters=params, enable_cross_partition_query=True
        ):
            # Cosmos VectorDistance with cosine returns similarity in [0,1] (1 = identical).
            # Order by VectorDistance returns most-similar first regardless of ASC/DESC.
            similarity = float(item.get("similarity", 0) or 0)
            doc = item.get("c", {}) or {}
            text, text_parts = _extract_text(doc, content_fields)

            metadata: Dict[str, Any] = dict(doc.get("metadata", {}) or {})
            for f in return_fields:
                if f in doc and f not in metadata:
                    metadata[f] = doc[f]

            hit: Dict[str, Any] = {
                "id": doc.get("id"),
                "score": similarity,
                "distance": 1 - similarity,
                "text": text,
                "text_parts": text_parts,
                "metadata": metadata,
                "source": doc.get("source"),
                "source_ref": doc.get("source_ref") or doc.get("title") or doc.get("url"),
            }
            if include_vector:
                hit["vector"] = doc.get(vf.split(".")[-1])
            hits.append(hit)
        return hits

    return await asyncio.to_thread(_run)


# =============================================================================
# Cosmos full-text search
# =============================================================================


_FTS_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_'\-]+")
_FTS_MAX_TERMS = int(os.getenv("SEARCH_FTS_MAX_TERMS", "16"))


def _tokenize_fts_query(query: str) -> List[str]:
    """Conservative tokenizer for Cosmos FullTextScore.

    Splits on non-alphanumeric (keeping hyphens, apostrophes, underscores),
    lowercases, drops empties + duplicates while preserving order, and caps
    at SEARCH_FTS_MAX_TERMS to bound SQL size + RU cost.
    """
    if not query:
        return []
    seen: Dict[str, None] = {}
    for tok in _FTS_TOKEN_RE.findall(query.lower()):
        if tok and tok not in seen:
            seen[tok] = None
            if len(seen) >= _FTS_MAX_TERMS:
                break
    return list(seen.keys())


async def search_cosmos_fts(
    store: CosmosStore,
    fts_field: str,
    query_terms: List[str],
    top_k: int,
    content_fields: List[str],
    return_fields: List[str],
    index_filter: Optional[IndexFilter],
    include_vector: bool,
) -> List[dict]:
    """Cosmos DB full-text search via `FullTextScore` (sync SDK wrapped).

    Requires the target container to have a full-text indexing policy on
    `fts_field`. FullTextScore can only appear in `ORDER BY RANK`, so the
    native BM25 score is not returned — hits get `score=None` and merge via
    rank-derived RRF.
    """
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    if not _FTS_FIELD_RE.match(fts_field):
        raise ValueError(f"invalid fts_field {fts_field!r}")
    if not query_terms:
        return []

    if getattr(store.auth, "mode", "managed_identity") == "key":
        key = await resolve_secret_ref(store.auth.secret_ref)  # type: ignore[attr-defined]
        client_args = {"credential": key}
    else:
        client_args = {"credential": DefaultAzureCredential()}

    def _run():
        client = CosmosClient(store.endpoint, **client_args)
        database = client.get_database_client(store.database)
        container = database.get_container_client(store.container)

        ff = fts_field.lstrip("/").replace("/", ".")

        params: List[dict] = [{"name": "@top_k", "value": int(top_k)}]
        term_placeholders: List[str] = []
        for i, term in enumerate(query_terms):
            ph = f"@term{i}"
            term_placeholders.append(ph)
            params.append({"name": ph, "value": term})

        where = ""
        if index_filter and index_filter.where:
            where = f"WHERE {index_filter.where}"
            if isinstance(index_filter.params, dict):
                for k, v in index_filter.params.items():
                    name = k if k.startswith("@") else f"@{k}"
                    params.append({"name": name, "value": v})

        term_args = ", ".join(term_placeholders)
        query = (
            f"SELECT TOP @top_k c FROM c {where} "
            f"ORDER BY RANK FullTextScore(c.{ff}, {term_args})"
        )

        hits: List[dict] = []
        for item in container.query_items(
            query=query, parameters=params, enable_cross_partition_query=True
        ):
            if isinstance(item, dict) and "c" in item:
                doc = item.get("c") or {}
            else:
                # Cosmos may return the doc inlined when only one column is projected.
                doc = item if isinstance(item, dict) else {}
            text, text_parts = _extract_text(doc, content_fields)

            metadata: Dict[str, Any] = dict(doc.get("metadata", {}) or {})
            for f in return_fields:
                if f in doc and f not in metadata:
                    metadata[f] = doc[f]

            hit: Dict[str, Any] = {
                "id": doc.get("id"),
                "score": None,  # FullTextScore not available in projection
                "text": text,
                "text_parts": text_parts,
                "metadata": metadata,
                "source": doc.get("source"),
                "source_ref": doc.get("source_ref") or doc.get("title") or doc.get("url"),
            }
            if include_vector:
                hit["vector"] = None
            hits.append(hit)
        return hits

    return await asyncio.to_thread(_run)


async def search_pgvector(
    store: PgVectorStore,
    vec_field: str,
    embedding: List[float],
    top_k: int,
    content_fields: List[str],
    return_fields: List[str],
    index_filter: Optional[IndexFilter],
    include_vector: bool,
    metric: str = "cosine",
) -> List[dict]:
    import asyncpg  # type: ignore

    if store.dsn:
        dsn = store.dsn
    elif store.dsn_secret_ref:
        dsn = await resolve_secret_ref(store.dsn_secret_ref)
    else:
        user = store.user or ""
        pw = store.password or ""
        dsn = f"postgresql://{user}:{pw}@{store.host}:{store.port}/{store.database}"
    ssl = store.ssl_mode not in ("disable", "allow")

    table = store.table
    id_col = store.id_column
    content_col = store.content_column
    vcol = vec_field
    if not isinstance(metric, str):
        metric = "cosine"
    metric = metric.lower()

    def q(c: str) -> str:
        return '"' + c.replace('"', '""') + '"'

    # Map metric → operator + similarity expression
    if metric in ("l2", "euclidean"):
        op = "<->"
        sim_expr = f"1.0 / (1.0 + ({q(vcol)} <-> $1::vector))"
    elif metric in ("dot", "inner_product", "ip"):
        op = "<#>"
        sim_expr = f"-({q(vcol)} <#> $1::vector)"
    else:
        op = "<=>"
        sim_expr = f"1 - ({q(vcol)} <=> $1::vector)"

    select_cols = [id_col, content_col]
    extra_cols = list(dict.fromkeys([*content_fields, *return_fields, *store.metadata_columns]))
    for c in extra_cols:
        if c not in select_cols:
            select_cols.append(c)

    col_list = ", ".join(q(c) for c in select_cols)

    # $1 = vector literal, $2 = top_k; filter params (if list) start at $3
    query_params: List[Any] = [str(list(embedding)), int(top_k)]
    where_clause = ""
    if index_filter and index_filter.where:
        where_clause = f"WHERE {index_filter.where}"
        if isinstance(index_filter.params, list):
            query_params.extend(index_filter.params)
        elif isinstance(index_filter.params, dict):
            raise ValueError(
                "pgvector filter.params must be a list (positional $3, $4, ...)"
            )

    sql = (
        f"SELECT {col_list}, {sim_expr} AS similarity "
        f"FROM {q(table)} {where_clause} "
        f"ORDER BY {q(vcol)} {op} $1::vector LIMIT $2"
    )

    conn = await asyncpg.connect(dsn, ssl=ssl if ssl else None)
    try:
        rows = await conn.fetch(sql, *query_params)
    finally:
        await conn.close()

    hits: List[dict] = []
    for row in rows:
        doc = dict(row)
        similarity = float(doc.pop("similarity", 0) or 0)

        present_content_fields = [f for f in content_fields if f in doc]
        if present_content_fields:
            text, text_parts = _extract_text(doc, present_content_fields)
        else:
            text = str(doc.get(content_col, "") or "")
            text_parts = None

        metadata: Dict[str, Any] = {}
        for c in [*return_fields, *store.metadata_columns]:
            if c in doc and c != content_col:
                metadata[c] = doc[c]

        hit: Dict[str, Any] = {
            "id": doc.get(id_col),
            "score": similarity,
            "distance": 1 - similarity,
            "text": text,
            "text_parts": text_parts,
            "metadata": metadata,
            "source": doc.get("source"),
            "source_ref": doc.get("source_ref"),
        }
        if include_vector:
            hit["vector"] = None  # Not fetched by default (expensive)
        hits.append(hit)
    return hits


async def _search_one_index(
    http: httpx.AsyncClient,
    idx: IndexSpec,
    default_per_index_top_k: int,
    query: Optional[str],
    request_id: str,
    include_vector: bool,
    query_image_b64: Optional[str] = None,
) -> Tuple[PerIndexInfo, List[dict]]:
    """Embed (if needed) + search one index. Never raises."""
    info = PerIndexInfo(index_id=idx.id)
    per_top_k = idx.top_k or default_per_index_top_k

    # -------------------------------------------------------------------------
    # FTS branch — no embedding call; Cosmos-only in v1.
    # -------------------------------------------------------------------------
    if idx.mode == "fts":
        info.embedding_model = "fts"
        if not query:
            info.error = "fts mode requires text query"
            return info, []
        if not isinstance(idx.store, CosmosStore):
            info.error = "fts mode only supports cosmosdb store in v1"
            return info, []
        terms = _tokenize_fts_query(query)
        if not terms:
            info.error = "fts mode: query produced no searchable tokens"
            return info, []
        fts_field = idx.fts_field or (idx.content_fields[0] if idx.content_fields else "content")
        t1 = time.time()
        try:
            hits = await asyncio.wait_for(
                search_cosmos_fts(
                    idx.store, fts_field, terms, per_top_k,
                    idx.content_fields, idx.return_fields, idx.filter, include_vector,
                ),
                timeout=PER_INDEX_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            info.error = f"per-index timeout > {PER_INDEX_TIMEOUT_S}s"
            info.search_ms = int((time.time() - t1) * 1000)
            return info, []
        except Exception as e:
            info.error = f"search: {str(e)[:180]}"
            info.search_ms = int((time.time() - t1) * 1000)
            return info, []
        info.search_ms = int((time.time() - t1) * 1000)
        info.result_count = len(hits)
        return info, hits

    # -------------------------------------------------------------------------
    # Vector branch — embed then search.
    # -------------------------------------------------------------------------
    if idx.embedding is None:
        info.error = "embedding policy required for vector mode"
        return info, []

    # Embed
    t0 = time.time()
    try:
        embedding, label = await embed_query(
            http, idx.embedding, query or "", request_id, query_image_b64=query_image_b64
        )
    except Exception as e:
        info.error = f"embed: {str(e)[:180]}"
        return info, []
    info.embedding_ms = int((time.time() - t0) * 1000)
    info.embedding_model = label
    info.embedding_dims = len(embedding)

    if idx.vector.dims and idx.vector.dims != len(embedding):
        info.error = (
            f"embedding dims mismatch: vector.dims={idx.vector.dims}, got={len(embedding)}"
        )
        return info, []

    # Search
    t1 = time.time()
    try:
        if isinstance(idx.store, CosmosStore):
            hits = await asyncio.wait_for(
                search_cosmos(
                    idx.store, idx.vector.field, embedding, per_top_k,
                    idx.content_fields, idx.return_fields, idx.filter, include_vector,
                ),
                timeout=PER_INDEX_TIMEOUT_S,
            )
        elif isinstance(idx.store, PgVectorStore):
            hits = await asyncio.wait_for(
                search_pgvector(
                    idx.store, idx.vector.field, embedding, per_top_k,
                    idx.content_fields, idx.return_fields, idx.filter, include_vector,
                    metric=idx.vector.metric,
                ),
                timeout=PER_INDEX_TIMEOUT_S,
            )
        else:
            info.error = f"unsupported store type: {type(idx.store).__name__}"
            return info, []
    except asyncio.TimeoutError:
        info.error = f"per-index timeout > {PER_INDEX_TIMEOUT_S}s"
        info.search_ms = int((time.time() - t1) * 1000)
        return info, []
    except Exception as e:
        info.error = f"search: {str(e)[:180]}"
        info.search_ms = int((time.time() - t1) * 1000)
        return info, []

    info.search_ms = int((time.time() - t1) * 1000)
    info.result_count = len(hits)
    return info, hits


# =============================================================================
# Merge
# =============================================================================


def _merge(
    per_index_hits: List[Tuple[str, List[dict]]], cfg: MergeConfig, top_k: int,
    fusion_groups: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Return merged list of hit dicts with fields: index_id, id, rank, score, rrf_score, ...

    fusion_groups maps index_id -> fusion_group. Under rrf, indexes sharing a
    fusion_group are fused on (fusion_group, doc_id) so the same document found by
    a vector spec and an fts spec over the same container (hybrid search) collapses
    into one result with a combined RRF score."""
    fusion_groups = fusion_groups or {}
    if cfg.strategy == "per_index":
        out: List[dict] = []
        for idx_id, hits in per_index_hits:
            for rank, h in enumerate(hits, start=1):
                h2 = dict(h)
                h2["index_id"] = idx_id
                h2["rank"] = rank
                out.append(h2)
        return out

    if cfg.strategy == "rrf":
        scores: Dict[Tuple[str, Any], Dict[str, Any]] = {}
        for idx_id, hits in per_index_hits:
            group = fusion_groups.get(idx_id) or idx_id
            for rank, h in enumerate(hits, start=1):
                key = (group, h.get("id"))
                contrib = 1.0 / (cfg.rrf_k + rank)
                if key not in scores:
                    entry = dict(h)
                    entry["index_id"] = group
                    entry["rrf_score"] = contrib
                    scores[key] = entry
                else:
                    scores[key]["rrf_score"] += contrib
        merged = sorted(scores.values(), key=lambda r: r["rrf_score"], reverse=True)
        for rank, h in enumerate(merged[:top_k], start=1):
            h["rank"] = rank
        return merged[:top_k]

    if cfg.strategy == "round_robin":
        out = []
        iters = [iter(h) for (_, h) in per_index_hits if h]
        idx_ids = [i for (i, h) in per_index_hits if h]
        while iters and len(out) < top_k:
            next_iters, next_ids = [], []
            for it, iid in zip(iters, idx_ids):
                if len(out) >= top_k:
                    break
                try:
                    h = next(it)
                    h2 = dict(h); h2["index_id"] = iid
                    out.append(h2)
                    next_iters.append(it); next_ids.append(iid)
                except StopIteration:
                    pass
            iters, idx_ids = next_iters, next_ids
        for rank, h in enumerate(out, start=1):
            h["rank"] = rank
        return out

    # "score": pool + sort by native similarity
    pool: List[dict] = []
    for idx_id, hits in per_index_hits:
        for h in hits:
            h2 = dict(h); h2["index_id"] = idx_id
            pool.append(h2)
    pool.sort(key=lambda r: r.get("score", 0) or 0, reverse=True)
    pool = pool[:top_k]
    for rank, h in enumerate(pool, start=1):
        h["rank"] = rank
    return pool


# =============================================================================
# Public entry points
# =============================================================================


async def run_search(http: httpx.AsyncClient, req: SearchRequest) -> SearchResponse:
    import uuid

    request_id = req.request_id or f"srv-{uuid.uuid4().hex[:12]}"
    t_total = time.time()

    tasks = [
        _search_one_index(
            http, idx, req.merge.per_index_top_k, req.query, request_id,
            include_vector=req.include.vectors,
            query_image_b64=req.query_image_b64,
        )
        for idx in req.indexes
    ]

    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=TOTAL_TIMEOUT_S)

    per_index_infos: List[PerIndexInfo] = []
    per_index_hits: List[Tuple[str, List[dict]]] = []
    max_embed_ms = 0
    max_search_ms = 0
    models_seen = set()
    errors: List[Tuple[str, str]] = []
    for info, hits in results:
        per_index_infos.append(info)
        per_index_hits.append((info.index_id, hits))
        if info.embedding_ms:
            max_embed_ms = max(max_embed_ms, info.embedding_ms)
        if info.search_ms:
            max_search_ms = max(max_search_ms, info.search_ms)
        if info.embedding_model and info.embedding_model != "precomputed":
            models_seen.add(info.embedding_model)
        if info.error:
            errors.append((info.index_id, info.error))

    warnings: List[str] = []
    if len(models_seen) > 1 and req.merge.strategy == "score":
        warnings.append(
            "mixed embedding models with merge.strategy=score; scores are not directly comparable — consider 'rrf'"
        )
    if req.merge.strategy == "score" and any(ix.mode == "fts" for ix in req.indexes):
        warnings.append(
            "merge.strategy=score with fts indexes will rank fts hits as None — use 'rrf' for hybrid"
        )

    if req.strict and errors:
        raise RuntimeError(
            "strict=true: " + "; ".join(f"{iid}: {err}" for iid, err in errors)
        )

    t_merge = time.time()
    fusion_groups = {ix.id: ix.fusion_group for ix in req.indexes if ix.fusion_group}
    merged = _merge(per_index_hits, req.merge, req.top_k, fusion_groups)
    merge_ms = int((time.time() - t_merge) * 1000)

    # Build index_id -> input_modality map for entity_type tagging.
    idx_modality: Dict[str, str] = {}
    idx_pipeline: Dict[str, str] = {}
    for ix in req.indexes:
        if ix.mode == "fts":
            mod = "text"
        else:
            mod = getattr(ix.embedding, "input_modality", "text") if ix.embedding else "text"
        idx_modality[ix.id] = mod
        pid = getattr(ix, "pipeline_id", None)
        if pid:
            idx_pipeline[ix.id] = pid

    def _classify(hit: dict) -> str:
        ref = (hit.get("source_ref") or hit.get("id") or "").lower()
        if any(ref.endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")):
            return "video"
        if any(ref.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
            return "image"
        # Fall back to the index's declared modality.
        mod = idx_modality.get(hit.get("index_id"), "text")
        return "image" if mod == "image" else "text"

    out_results: List[SearchResult] = []
    for h in merged:
        md = dict(h.get("metadata", {}) or {})
        pid = idx_pipeline.get(h.get("index_id"))
        if pid and "pipeline_id" not in md:
            md["pipeline_id"] = pid
        out_results.append(SearchResult(
            index_id=h["index_id"],
            id=h.get("id"),
            rank=h.get("rank", 0),
            score=(h.get("score") if req.include.scores else None),
            rrf_score=h.get("rrf_score"),
            text=h.get("text", "") or "",
            text_parts=h.get("text_parts"),
            metadata=md,
            vector=(h.get("vector") if req.include.vectors else None),
            source=h.get("source"),
            source_ref=h.get("source_ref"),
            entity_type=_classify(h),
        ))

    timing = SearchTiming(
        embed=max_embed_ms,
        search=max_search_ms,
        merge=merge_ms,
        total=int((time.time() - t_total) * 1000),
    )

    return SearchResponse(
        request_id=request_id,
        query=req.query,
        results=out_results,
        per_index=per_index_infos,
        merge=req.merge,
        warnings=warnings,
        timing_ms=timing,
    )


async def explain_search(req: SearchRequest) -> Dict[str, Any]:
    """Dry-run: return the resolved plan without executing embedding/search."""
    plan = []
    for idx in req.indexes:
        if idx.mode == "fts":
            fts_field = idx.fts_field or (idx.content_fields[0] if idx.content_fields else None)
            plan.append({
                "index_id": idx.id,
                "mode": "fts",
                "store_type": idx.store.type,
                "fts_field": fts_field,
                "fts_terms": _tokenize_fts_query(req.query or ""),
                "embedding": None,
                "content_fields": idx.content_fields,
                "return_fields": idx.return_fields,
                "has_filter": idx.filter is not None,
                "top_k": idx.top_k or req.merge.per_index_top_k,
            })
            continue
        emb = idx.embedding
        if isinstance(emb, ModelEmbedding):
            emb_desc = {"policy": "model", "model_id": emb.model_id, "normalize": emb.normalize}
        elif isinstance(emb, PipelineEmbedding):
            emb_desc = {"policy": "pipeline", "pipeline": emb.pipeline, "normalize": emb.normalize}
        elif isinstance(emb, PrecomputedEmbedding):
            emb_desc = {"policy": "precomputed", "dims": len(emb.vector)}
        else:
            emb_desc = None
        plan.append({
            "index_id": idx.id,
            "mode": "vector",
            "store_type": idx.store.type,
            "vector_field": idx.vector.field,
            "metric": idx.vector.metric,
            "embedding": emb_desc,
            "content_fields": idx.content_fields,
            "return_fields": idx.return_fields,
            "has_filter": idx.filter is not None,
            "top_k": idx.top_k or req.merge.per_index_top_k,
        })
    return {
        "request_id": req.request_id or "dry-run",
        "query": req.query,
        "final_top_k": req.top_k,
        "merge": req.merge.model_dump(),
        "indexes": plan,
        "embedding_service": DOCGROK_URL,
        "limits": {
            "per_index_timeout_s": PER_INDEX_TIMEOUT_S,
            "total_timeout_s": TOTAL_TIMEOUT_S,
        },
    }
