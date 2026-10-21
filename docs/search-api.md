# OmniVec Search API

[README](../README.md) | [User guide](user-guide.md) | [Architecture](architecture.md)

Choose the API that matches your client:

| API | Use when | Authentication | Address |
|-----|----------|----------------|---------|
| Control-plane `POST /api/playground/search` | You have registered OmniVec destinations/pipelines | Normal OmniVec API credential | The console/API origin |
| Standalone `POST /search` | Your application supplies complete index and embedding specifications | Search-scoped bearer token | `omnivec-search` service; ClusterIP by default |

The console and `omnivec search` use the first route. The control plane calls
standalone search internally; there is no `/api/search` route.
Do not assume a public search IP exists. For external clients, configure a
deliberate TLS-protected ingress/gateway or use the control-plane resolver.

## Search registered destinations

Send this body to `POST /api/playground/search` with
`Authorization: Bearer <control-plane-token>`:

```json
{
  "query": "How do vector indexes work?",
  "destination_ids": ["dst-example"],
  "top_k": 5,
  "search_mode": "vector",
  "merge_strategy": "rrf"
}
```

Replace the destination ID with one from your installation. The resolver uses
pipeline/model metadata to create index specifications; vector dimensions and
the embedding model must match those used during ingestion.

Optional `source_id`, `pipeline_id`, and `source_ref` fields scope supported
searches. Choose the intended pipeline when several pipelines share a destination.
The service must have `SEARCH_INTERNAL_TOKEN` configured in both the API and
search workloads.

| Mode | Supported stores | Requirements |
|------|------------------|--------------|
| `vector` | Cosmos DB, pgvector, Garnet, OneLake with Garnet mirror | Matching query model and vector data |
| `fts` | Cosmos DB | Text query, full-text policy/index, and correct `fts_field` |
| `hybrid` | Cosmos DB vector + full-text | Both policies; uses rank fusion |

SQL Server destinations, null sinks, plain OneLake write-back, and the legacy
Redis string mirror are not searchable through OmniVec.

The resolver permits up to ten resolved index specifications. Hybrid creates
two per Cosmos destination, so five such destinations already use that limit.
Full-text/hybrid mode uses RRF even if another merge strategy was requested.
The UI alias `interleave` maps to `round_robin` for vector mode.

Responses contain `results`, `indexes_searched`, `total_search_time_ms`, and
`warnings`. Inspect each index's `error` as well as top-level warnings. A
successful response may be partial. Scores and fusion scores are ranking
signals, not calibrated probabilities of relevance.

## Standalone service access

Default in-cluster address:
`http://omnivec-search.<namespace>.svc.cluster.local`.
The service listens through the Helm service on port 80.

### Tokens

All `/search*` routes require `Authorization: Bearer <search-token>`.
Do not reuse an admin/control-plane token. Rejected tokens normally return
`401`; a token reaching the scope check with an incompatible scope returns `403`.

| Token source | Configuration |
|--------------|---------------|
| Bootstrap search token | `OMNIVEC_SEARCH_TOKEN` on search |
| Internal API-to-search token | `SEARCH_INTERNAL_TOKEN` on API and search; not for distribution to clients |
| Per-client search token | Created by an OmniVec administrator and stored as a hashed metadata record |

Create a per-client token through **the control-plane API**, not the search host:

```text
POST /api/auth/tokens
Authorization: Bearer <admin-token>
Content-Type: application/json
```

```json
{
  "name": "search-client",
  "role": "user",
  "scope": "search",
  "expires_days": 90
}
```

Save the returned token securely; it is shown only at creation. List metadata
with `GET /api/auth/tokens?scope=search` and revoke it with
`DELETE /api/auth/tokens/{token_id}`, using an administrator credential.
Search-scoped tokens do not authorize control-plane administration.

### Endpoints

| Method | Path | Authentication | Purpose |
|--------|------|----------------|---------|
| `GET` | `/health` | None | Process liveness |
| `GET` | `/ready` | None | DocGrok reachability; does not test every store |
| `GET` | `/schema` | None | Current `SearchRequest` JSON Schema |
| `POST` | `/search` | Search token | Execute retrieval |
| `POST` | `/search/explain` | Search token | Inspect the plan without embedding or store queries |

## Standalone request

Every index describes its store, vector field, and embedding policy. The service
does not resolve OmniVec destination IDs itself.

```json
{
  "query": "How do vector indexes work?",
  "top_k": 5,
  "indexes": [
    {
      "id": "documents",
      "store": {
        "type": "cosmosdb",
        "endpoint": "https://example.documents.azure.com",
        "database": "demo",
        "container": "vectors",
        "auth": {"mode": "managed_identity"}
      },
      "vector": {
        "field": "embedding",
        "dims": 1536,
        "metric": "cosine"
      },
      "embedding": {
        "policy": "model",
        "model_id": "mdl-example"
      },
      "content_fields": ["content"],
      "return_fields": ["source_id", "source_ref", "pipeline_id"]
    }
  ],
  "merge": {"strategy": "rrf", "rrf_k": 60},
  "include": {"vectors": false, "scores": true},
  "strict": false,
  "request_id": "example-search-1"
}
```

Replace the endpoint and model ID with accessible, registered resources.
`content_fields` must name text actually persisted during ingestion; for chunk
pipelines that may be `text` rather than `content`.

### Embedding policies

| `policy` | Required setting | Behavior |
|----------|------------------|----------|
| `model` | `model_id` | Embed through the selected registered model |
| `pipeline` | `pipeline` | Embed through a named DocGrok transform |
| `precomputed` | Numeric `vector` array | Skip embedding for that index |

The current request schema requires `query` or `query_image_b64` **even when all
indexes use precomputed vectors**. Supply a query description in that case;
it is not embedded by a precomputed policy. A precomputed vector must contain
the full numeric array with the correct dimensions, not an ellipsis or string.

For image-capable models, use the appropriate `input_modality` in the embedding
policy and a text or base64 image query supported by that model. Image-only
queries cannot drive text full-text search.

### Stores

| `store.type` | Required configuration | Notes |
|--------------|------------------------|-------|
| `cosmosdb` | `endpoint`, `database`, `container` | Workload identity or supported Key Vault-backed key auth; vector or full-text |
| `pgvector` | `table` plus `dsn`, `dsn_secret_ref`, or `host`/`database` and applicable credentials | Vector only; configure ID/content/vector columns |
| `garnet` | `endpoint`, optional `vector_set` | Preview Vector Set server, not generic Redis; vector only |

Use `kv://<vault>/<secret>` where the schema supports secret references. The
search workload must be able to read the referenced secret. Avoid embedding
literal passwords or DSNs in shared requests/logs.

For Garnet, TLS defaults to true, `use_entra_auth` defaults to false, and ACL
authentication uses `username` plus `auth: {"mode": "key", "secret_ref": "kv://..."}`.
Only enable Entra when the endpoint implements that token flow. See
[Garnet setup](user-guide.md#garnet-vector-sets).

### Filters

Filter syntax is store-specific; it is not a portable query language:

| Store | `filter.where` | Parameters |
|-------|----------------|------------|
| Cosmos | Clause fragment using alias `c`, without `WHERE` | Named `@parameters` in a dictionary |
| pgvector | SQL clause fragment | Positional values starting at `$3`; `$1`/`$2` are reserved |
| Garnet | Native Vector Set `FILTER` expression | Parameter substitution is unsupported |

Example Cosmos filter:

```json
{
  "where": "c.pipeline_id = @pipeline",
  "params": {"@pipeline": "pip-example"}
}
```

These are application-supplied queries, not an authorization boundary. Apply
your application's access policy before constructing a request.

### Full-text and hybrid requests

A standalone index uses `mode: "vector"` (default) or `mode: "fts"`.
Full-text mode is Cosmos-only and requires `fts_field`, or a suitable first
`content_fields` entry. It does not require an embedding policy.

For hybrid retrieval, submit vector and full-text specifications for the same
container, use RRF, and give them the same `fusion_group`. This lets the service
fuse the same document rather than return one copy per specification.

### Merge strategies

| Strategy | Behavior |
|----------|----------|
| `rrf` | Reciprocal Rank Fusion; default `rrf_k=60`, suitable for different ranking scales |
| `score` | Sort by native score; mixed models/scoring systems may not be comparable |
| `round_robin` | Interleave results from each index |
| `per_index` | Keep per-index ordering instead of globally ranking by similarity |

`merge.per_index_top_k` defaults to 20; an index's `top_k` can override its
candidate count. The request's `top_k` limits final results.

## Responses, limits, and failures

Standalone responses include `results`, `per_index`, `merge`, `warnings`, and
`timing_ms`. `per_index` reports embedding/search timing, result count, and any
error for each index. The response preserves the request ID.

With `strict: false` (default), a failed index can return no hits and an `error`
inside an otherwise HTTP `200` response. That can include missing stores,
dimension mismatches, embedding failures, or timeouts. Inspect every index even
when results are empty. With `strict: true`, any per-index failure rejects the
request with `422`.

| Limit | Default / range |
|-------|-----------------|
| Index specifications | 1-10 |
| Final `top_k` | 1-100 |
| Per-index candidate count | 1-500 |
| Embedding timeout | 10 s, `SEARCH_EMBED_TIMEOUT_S` |
| Store query timeout per index | 5 s, `SEARCH_PER_INDEX_TIMEOUT_S` |
| Total request timeout | 15 s, `SEARCH_TOTAL_TIMEOUT_S` |

Vector dimensions must match the chosen model and destination; consult the
destination's own limits. Do not infer a universal dimension cap from this table.

| HTTP status | Typical meaning |
|-------------|-----------------|
| `400` | Request rejected during execution/semantic validation |
| `401` | Missing, invalid, expired, or rejected token |
| `403` | Incompatible credential scope |
| `422` | Schema validation, or strict-mode index failure |
| `429` | Rate limit; respect `Retry-After` |
| `502` | Request-level runtime/dependency failure |
| `504` | Total request timeout |

`/search/explain` uses the same request schema and authentication, but does not
run embedding or store queries. It cannot prove connectivity, permissions,
actual vector dimensions, or result quality.

## Rate limits and browser access

`SEARCH_RATE_LIMIT_RPM` defaults to `0` (disabled). Buckets are per stored-token
ID, with subject fallback for bootstrap/internal tokens, and are held **per pod**.
A configured per-token override can change the limit. Use a gateway for a strict
cross-replica limit.

`SEARCH_CORS_ORIGINS` is a comma-separated allowlist and defaults to `*`.
Restrict it to intended browser origins when exposing search. CORS does not
replace authentication, network policy, TLS, or application authorization.
