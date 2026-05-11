# OmniVec Data Classification

> **Status:** living document. Updated when new doc-types or destinations are added.
> **Owner:** OmniVec security WG. **Last revised:** batch 3, threat-model follow-up.

This document classifies every persistent data store that OmniVec writes to,
so operators can apply the correct retention, access-control, and erasure
policies. It addresses threat-model item **T-VEC-1**.

## 1. Classification scheme

We use a four-tier scheme aligned with the Microsoft Data Classification
Standard. Each tier dictates retention, encryption, access-review cadence,
and erasure obligations.

| Tier | Examples | Retention default | Erasure SLA |
|------|----------|-------------------|-------------|
| **Public** | Marketing assets, SDK samples | indefinite | n/a |
| **Internal** | Pipeline metadata, source configs (no secrets), metrics | 2 years rolling | 30 days |
| **Confidential** | Token hashes, audit logs, model API keys (until KV migration) | 1 year | 7 days |
| **Confidential / PII** | Embedded chunk content, source documents, attachments | 90 days rolling unless customer-pinned | **72 h on validated subject request** |

Anything labelled **Confidential / PII** is subject to GDPR / CCPA-style
data-subject access and deletion requests.

## 2. Per-store inventory

### 2.1 Cosmos `omnivec` database (control-plane metadata)

| Container | Doc-type(s) | Tier | Notes |
|-----------|-------------|------|-------|
| `metadata` | `source`, `destination`, `pipeline`, `assistant`, `docgrok_model`, `metrics` | **Internal** | Configs only — no document bodies, no embeddings. Source connection strings are pulled from Key Vault at runtime. |
| `metadata` | `auth_token` | **Confidential** | Stores token hash + role + scope + last-used. Plain token never persisted. |
| `metadata` | `audit_log` | **Confidential** | Actor, method, path (no body), status, IP, timestamp. **Recommended Cosmos TTL: 365 days.** |
| `metadata` | `job` | **Internal** | Pipeline job state. May reference attachment blob keys but never embed content. |

### 2.2 Cosmos `e2eblob` database (vector destination, default)

| Container | Doc-type | Tier | Notes |
|-----------|----------|------|-------|
| `vectors` | embedded chunks | **Confidential / PII** | Each doc carries `id`, `source_ref`, `embedding` vector, `embedded_at`, plus passthrough source content fields (e.g. `summary`, `title`). The vector itself is reversible to a degree depending on the embedding model — **must be treated as the source content**. |

### 2.3 Postgres pgvector destinations (customer-managed)

Same classification as 2.2 — **Confidential / PII**. Schema is operator-defined;
default columns: `id`, `source_id`, `source_ref`, `content`, `embedding`,
`created_at`. The `content` column is plain text — high-sensitivity.

### 2.4 Blob attachment cache (`omnivec-attachments` container)

| Path | Tier | Notes |
|------|------|-------|
| `pdf/{docId}::{attName}` etc. | **Confidential / PII** | Verbatim copies of customer documents. Lifecycle policy: delete after 30 days. |

### 2.5 Service-Bus / Queue Storage messages

| Queue | Tier | Notes |
|-------|------|-------|
| `embedding-jobs` | **Confidential / PII** | Job payloads include either inline content or a blob_ref. Messages are short-lived (TTL 7 days). |

### 2.6 Logs (App Insights / stdout)

**Internal**, after `_SensitiveFilter` redacts secrets and after `_redact_path`
strips query strings on audit entries. Long retention (90 days default in
App Insights) — no document bodies are logged anywhere.

## 3. Erasure / right-to-delete workflow

Until a turnkey purge-by-source endpoint ships, operators delete a tenant's
data manually:

1. **Identify the source(s)** belonging to the data subject:
   `GET /api/sources?owner=<id>` (or filter by tag).
2. **Disable ingestion**: `PATCH /api/sources/{id}` set `status=paused`.
3. **Delete vectors** — use the relevant connector tool:
   - Cosmos vector destination: `connectors.cosmosdb_vector_connector.delete_chunks_by_prefix(config, prefix=f"{pipeline_id}-")`.
     This deletes by pipeline; if multiple sources feed one pipeline, all
     are removed (acceptable when isolating a tenant; not when surgical).
   - Postgres: `DELETE FROM <vectors_table> WHERE source_id = $1` (run via
     the `psql` operator console — there is no admin endpoint yet).
4. **Delete attachment cache**: `az storage blob delete-batch
   --source omnivec-attachments --pattern "*::{source_id}*"`.
5. **Delete source metadata**: `DELETE /api/sources/{id}`.
6. **Confirm**: `GET /api/audit-log?actor=<operator>&since=<ts>` should show
   the chain of `DELETE` calls.

A future PR (T-VEC-1 follow-up) will collapse steps 3–5 into a single
`DELETE /api/sources/{id}/vectors?cascade=true` call.

## 4. Access control summary

| Role | Can read | Can write | Can delete |
|------|----------|-----------|------------|
| `viewer` | metadata + metrics | — | — |
| `operator` | metadata + metrics + audit_log | sources, pipelines, jobs | own jobs |
| `admin` | everything (incl. tokens) | everything | everything |

Roles are enforced in `AuthMiddleware` + per-endpoint `request.state.auth.role`
checks. Token records are never returned with the plain token after creation
(only `id`, `name`, `role`, `created_at`, `last_used_at`, `use_count`).

## 5. Open items

- **Right-to-erasure endpoint** (`DELETE /api/sources/{id}/vectors`) — tracked
  under T-VEC-1; not yet shipped.
- **Cosmos TTL on `audit_log`** — set to 365 days at deployment time
  (`az cosmosdb sql container update --idx ...defaultTtl=31536000`).
- **PII-aware embedding model selection** — embeddings of customer text in
  shared OpenAI deployments leak content via embedding inversion attacks.
  Use a customer-pinned deployment (`source.docgrok_pipeline.embedding_model`)
  for tenants with PII contracts.
