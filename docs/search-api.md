# OmniVec Search API

The OmniVec Search service is a standalone, externally-exposed microservice that
performs multi-index vector similarity search across heterogeneous vector stores
(Azure Cosmos DB, pgvector) with per-index embedding policies.

It runs as its own Kubernetes Deployment (`omnivec-search`) behind a dedicated
`LoadBalancer` Service with its own public IP, and scales independently via HPA.
It is **not** served through the control-plane `api.py`.

- In-cluster DNS: `http://omnivec-search.<namespace>.svc.cluster.local`
- External: `http://<search-loadbalancer-ip>/`

## Authentication

All `/search*` endpoints require a bearer token in the `Authorization` header.
Search tokens are **distinct** from admin tokens — an admin token will not
authenticate against the search service (returns `403`), and a search token
will not authenticate against `api.py` admin endpoints (returns `403`).

| Token | Env var | Scope | Source |
|---|---|---|---|
| Bootstrap | `OMNIVEC_SEARCH_TOKEN` | `search` | search pod env |
| Internal s2s | `SEARCH_INTERNAL_TOKEN` | `search` | shared with api pod |
| Per-client | (stored in Cosmos `metadata` container) | `search` | created via admin API |

### Create a per-client search token

```bash
curl -X POST https://<api-host>/api/auth/tokens \
  -H "Authorization: Bearer $OMNIVEC_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"playground","role":"user","scope":"search","expires_days":90}'
```

List search tokens:

```bash
curl "https://<api-host>/api/auth/tokens?scope=search" \
  -H "Authorization: Bearer $OMNIVEC_ADMIN_TOKEN"
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/health`         | no  | Liveness. |
| `GET`  | `/ready`          | no  | Readiness (checks DocGrok reachability). |
| `GET`  | `/schema`         | no  | JSON Schema for `SearchRequest`. |
| `POST` | `/search`         | yes | Multi-index vector search. |
| `POST` | `/search/explain` | yes | Dry-run: returns resolved plan without executing queries. |

## `POST /search`

Each index in the request is **self-describing**: it carries its own store
configuration, vector column, and embedding policy. A single request can fan
out across heterogeneous stores using different embedding models.

### Embedding policies

| `policy` | Fields | Behaviour |
|---|---|---|
| `model`       | `model_id`  | Calls DocGrok `/embed` with `{model_id, text}`. |
| `pipeline`    | `pipeline`  | Calls DocGrok `/embed` with `{pipeline, text}`. |
| `precomputed` | `vector`    | Skips embed call for that index. |

If every index in a request uses `precomputed`, `query` is optional.

### Store types

`cosmosdb` and `pgvector`. Secrets (pgvector DSN, passwords) can be passed
literally or as Key Vault references of the form `kv://<vault>/<secret>`.

### Merge strategies

| `strategy`    | Notes |
|---|---|
| `rrf` (default) | Reciprocal Rank Fusion. Model-agnostic. `rrf_k` default 60. |
| `score`         | Sort by native similarity. Emits a warning on mixed embedding models. |
| `round_robin`   | One from each index, then repeat. |
| `per_index`     | No merge â€” groups results in `per_index`. |

### Limits

- Up to **10** indexes per request.
- `top_k` in `1..100`.
- Per-index timeout: **5 s** (`SEARCH_PER_INDEX_TIMEOUT_S`).
- Total timeout: **15 s** (`SEARCH_TOTAL_TIMEOUT_S`).
- Max vector dimensions: **4096**.

### Example request

```bash
curl -X POST https://<search-host>/search \
  -H "Authorization: Bearer $SEARCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "how do vector indexes work?",
    "top_k": 10,
    "merge": {"strategy": "rrf", "rrf_k": 60, "per_index_top_k": 20},
    "indexes": [
      {
        "id": "docs-cosmos",
        "store": {
          "type": "cosmosdb",
          "endpoint": "https://acct.documents.azure.com:443/",
          "database": "omnivec",
          "container": "docs",
          "auth": {"mode": "managed_identity"}
        },
        "vector": {"field": "embedding", "dims": 1024, "metric": "cosine"},
        "embedding": {"policy": "model", "model_id": "mdl-bge-large", "normalize": true},
        "content_fields": ["title", "body"],
        "return_fields": ["url", "tenant"],
        "filter": {"where": "c.tenant = @tenant", "params": {"@tenant": "acme"}},
        "top_k": 20
      },
      {
        "id": "tickets-pg",
        "store": {
          "type": "pgvector",
          "dsn_secret_ref": "kv://omnivec-kv/pg-tickets",
          "table": "tickets",
          "id_column": "id",
          "content_column": "body"
        },
        "vector": {"field": "embedding_bge", "dims": 1024, "metric": "cosine"},
        "embedding": {"policy": "precomputed", "vector": [0.01, -0.02, "..."]},
        "content_fields": ["body"]
      }
    ],
    "include": {"vectors": false, "scores": true, "debug": false},
    "request_id": "client-uuid"
  }'
```

### Example response

```json
{
  "request_id": "client-uuid",
  "query": "how do vector indexes work?",
  "results": [
    {
      "index_id": "docs-cosmos",
      "id": "doc-abc",
      "rank": 1,
      "score": 0.87,
      "rrf_score": 0.0163,
      "text": "...",
      "text_parts": [{"field": "title", "value": "..."}],
      "metadata": {"url": "..."},
      "vector": null
    }
  ],
  "per_index": [
    {
      "index_id": "docs-cosmos",
      "embedding_model": "mdl-bge-large",
      "embedding_dims": 1024,
      "embedding_ms": 42,
      "search_ms": 88,
      "result_count": 20,
      "error": null
    }
  ],
  "merge": {"strategy": "rrf", "rrf_k": 60},
  "warnings": [],
  "timing_ms": {"embed": 82, "search": 91, "merge": 2, "total": 180}
}
```

### Partial failures

A per-index failure (store timeout, unreachable pgvector, embed error) does
**not** fail the whole request. The response is still `200` and the failing
index appears in `per_index` with `error` populated and `result_count = 0`.

## `POST /search/explain`

Returns the resolved plan (stores, embedding dispatch, merge config, limits)
without executing any vector queries. Useful for validating a request body
without consuming store quota.

## Errors

| Status | Meaning |
|---|---|
| `400` | Invalid schema, zero indexes, >10 indexes, unknown store type, bad top_k. |
| `401` | Missing or malformed bearer token. |
| `403` | Token scope mismatch (e.g. admin token used here). |
| `404` | Container/table referenced in an index does not exist. |
| `429` | Rate limit exceeded. Respect `Retry-After`. |
| `502` | DocGrok unreachable. |
| `504` | Total request timeout. |

## Rate limits

Set per-subject via `SEARCH_RATE_LIMIT_RPM` (default `0` = disabled). Enforced
in-process per pod; for strict limits across replicas, put an API gateway
in front.

## CORS

`SEARCH_CORS_ORIGINS` (comma-separated). Default `*`. Set to the Playground
origin in production.
