# OmniVec Threat Model

| Field | Value |
|---|---|
| Owner | OmniVec Team |
| Methodology | STRIDE per element + per trust-boundary crossing |
| Last reviewed | 2026-05-05 |
| Scope | Full system — ingestion → embedding → vector store → search → web UI |

> If you prefer the Microsoft Threat Modeling Tool (TMT) format, paste the
> **Mermaid DFD** below into TMT manually — auto-generating a valid `.tm7`
> from scratch is brittle, and the markdown form below is the source of
> truth that gets diff-reviewed in PRs.

---

## 1. Architecture & data flow (DFD)

```mermaid
flowchart LR
  subgraph "INTERNET / AAD trust boundary"
    user["End-user browser"]
  end

  subgraph "AKS cluster (omnivec namespace)"
    web["omnivec-web<br/>(Next.js)"]
    api["omnivec-api<br/>(FastAPI)"]
    search["omnivec-search<br/>(Go)"]
    router["docgrok-router<br/>(Rust)"]
    pworker["docgrok-pipeline-worker"]
    connector["connector .NET worker<br/>(change-feed watcher)"]
    incluster["in-cluster embedders<br/>CLIP / BGE / DSE-Qwen2"]
  end

  subgraph "Azure managed services"
    aoai["Azure OpenAI"]
    cmeta["CosmosDB<br/>omnivec.metadata"]
    cvec["CosmosDB<br/>e2eblob.vectors"]
    blob["Azure Blob<br/>(attachment store)"]
    kv["Azure Key Vault"]
    sb["Azure Service Bus"]
  end

  subgraph "Customer-owned (external trust)"
    csrc["Customer CosmosDB<br/>(source w/ attachments)"]
    bsrc["Customer Blob source"]
  end

  user -->|HTTPS + AAD SSO| web
  user -->|HTTPS + admin token| api
  web --> api
  web --> search
  api --> cmeta
  api --> kv
  api --> sb
  api --> router
  search --> cvec
  router -->|API key OR AAD| aoai
  router --> incluster
  router --> cmeta
  router --> pworker
  pworker --> sb
  pworker --> bsrc
  pworker --> blob
  pworker --> router
  pworker --> cvec
  connector -->|change-feed read| csrc
  connector -->|stage attachments| blob
  connector -->|enqueue work| sb
```

## 2. Trust boundaries

| Id | Boundary | Notes |
|---|---|---|
| TB-1 | Internet / AAD | Browser-side input, AAD SSO. |
| TB-2 | AKS cluster | All cluster pods share NetworkPolicy default-deny + workload-identity SA bindings. |
| TB-3 | Azure managed services | RBAC data-plane on each resource. Public-network-access enabled on AOAI / Blob / Cosmos today (no private endpoints). |
| TB-4 | Customer-owned | We trust nothing about customer Cosmos / Blob: data, schemas, attachment names, MIME types, content. |

## 3. Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Customer document content (PDFs, Office, images) | High (may be PII) | Customer Blob → AKS (transient) → never persisted raw |
| Vector embeddings of customer content | High (PII-derived) | `e2eblob.vectors` |
| Source-of-truth pipeline / model definitions | Medium | `omnivec.metadata` |
| **AOAI API keys** | High | `omnivec.metadata` model records (`api_key` field) ⚠️ |
| Admin bearer token (`OMNIVEC_ADMIN_TOKEN`) | High | Pod env var; long-lived, no rotation ⚠️ |
| Workload-identity federated credentials | High | AAD; rotated by AKS |
| Service Bus messages | Medium (contain blob URLs + source IDs) | Service Bus queue |

## 4. STRIDE — auto-generated risks (one row per applicable element)

Legend: ✅ has mitigation in code · ⚠️ partial · ❌ open · — N/A

### Processes

| Element | S | T | R | I | D | E | Mitigations / Notes |
|---|---|---|---|---|---|---|---|
| omnivec-web | ⚠️ | ✅ | ✅ | ✅ | ⚠️ | ✅ | AAD SSO; CSP and output-encoding; **rate-limit at ingress is needed** |
| omnivec-api | ⚠️ | ✅ | ✅ | ✅ | ⚠️ | ✅ | Pydantic schemas; admin token static (T-API-1) |
| omnivec-search | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | Read-only RBAC on vectors; query length cap |
| docgrok-router | ⚠️ | ✅ | ⚠️ | ❌ | ⚠️ | ✅ | Loads AOAI keys from Cosmos in-the-clear (T-RTR-1) |
| pipeline-worker | ✅ | ✅ | ⚠️ | ✅ | ❌ | ⚠️ | Untrusted input parser; **needs sandboxing** (T-PWK-1) |
| connector .NET | ✅ | ⚠️ | ❌ | ✅ | ⚠️ | ✅ | Lease container shared (T-CON-1); SSRF surface (T-CON-2) |

### Data stores

| Element | S | T | R | I | D | E | Mitigations |
|---|---|---|---|---|---|---|---|
| `omnivec.metadata` | — | ✅ | ✅ | ⚠️ | ✅ | ✅ | RBAC; **api_key field stored in cleartext** (T-MET-1) |
| `e2eblob.vectors` | — | ✅ | ✅ | ⚠️ | ✅ | ✅ | RBAC; vectors are PII-derived → residency rules apply |
| Blob (attachment store) | — | ✅ | ⚠️ | ⚠️ | ✅ | ✅ | RBAC; SAS short-lived; **no path-traversal allowlist** (T-BLB-1) |
| Service Bus | — | ✅ | ✅ | ✅ | ✅ | ✅ | RBAC; dead-letter on poison messages |
| Key Vault | — | ✅ | ✅ | ✅ | ✅ | ✅ | RBAC + soft-delete + purge protection |

### External interactors / cross-trust flows

| Crossing | Risk |
|---|---|
| Browser → omnivec-api | Static admin-token replay (T-API-1) |
| omnivec-api → AOAI | Key-in-cleartext exfiltration if metadata DB breached (T-RTR-1, T-MET-1) |
| pipeline-worker → customer Blob | Oversized / malformed-doc DoS, parser RCE (T-PWK-1) |
| connector → customer Cosmos | Cross-tenant ingress, change-feed lease takeover (T-CON-1) |
| connector attachment-resolver → arbitrary blob URL | SSRF if URL not allowlisted (T-CON-2) — partially mitigated by PR #128 |

## 5. Top open risks (project-specific)

> These are the ones a generic STRIDE template will **not** surface — they
> require knowledge of this codebase. Prioritize these.

### T-API-1 — Static admin bearer token
- **Where:** `OMNIVEC_ADMIN_TOKEN` env var on `omnivec-api`. Single secret, no rotation, no per-call audit, used by web and CLI.
- **Risk:** S/E/R — anyone with the token has full admin. No revocation mid-life.
- **Mitigation:** migrate to AAD bearer (already done for cluster→Azure); add RBAC roles inside the API; rotate the token as a breakglass-only path.

### T-RTR-1 / T-MET-1 — AOAI API keys live in CosmosDB metadata
- **Where:** `omnivec.metadata.docgrok_model.api_key` is a **plaintext string** on the doc (we hit this today — that's why `mdl-ext-475483bb` was 401: empty `api_key`).
- **Risk:** I/T — Cosmos breach or misissued read role exfiltrates keys. Also — **rotation requires a Cosmos `replace_item`**, no automation.
- **Mitigation:** (a) drop key auth entirely now that AAD RBAC is granted (we did this 2026-05-05); (b) if keys must remain, store a Key Vault reference (`kv://…`) and have the router resolve it at request time.

### T-PWK-1 — Pipeline-worker parses untrusted documents in-process
- **Where:** `docgrok-pipeline-worker` ingests customer PDFs / Office / images via Python parsers (PyMuPDF, python-docx, Pillow). Same memory space as the embedding HTTP client.
- **Risk:** D/E — a single malicious doc can OOM the pod or escape the parser (CVEs in Pillow/PyMuPDF appear yearly).
- **Mitigation:** run parsing in a one-shot subprocess with `RLIMIT_AS` and a `seccomp` profile; impose hard page count + bytes cap before embed.

### T-CON-1 — Change-feed lease container shared with metadata
- **Where:** `connector .NET worker` uses Cosmos change-feed; if the lease container is in the same DB and writable by the same SA, an attacker who lands a write can DoS or replay ingestion.
- **Mitigation:** isolate lease container to its own DB / RBAC scope; minimum required permissions only.

### T-CON-2 — Attachment-source SSRF surface
- **Where:** `Source.cs` resolves relative attachment URLs against an account derived from source config. PR #128 split `attachment_blob_container` from `container` to fix one confusion class.
- **Residual risk:** the resolved blob URL is **not allowlisted by account name** — a malicious `_attachment.media` value could point at an attacker-controlled storage account that we then download from.
- **Mitigation:** require `attachment_blob_account` config and reject URLs whose host doesn't match; OR pin to private endpoint only.

### T-BLB-1 — No attachment-name validation
- **Where:** Blob keys are built from `{docId}/{attachmentId}` taken from customer Cosmos. No normalization.
- **Risk:** path-traversal-style `id="../foo"` could collide with system blobs, or create blobs we don't expect.
- **Mitigation:** sanitize/encode attachment IDs (e.g., percent-encode), reject `/` and `..`.

### T-VEC-1 — Vector PII residency
- **Where:** `e2eblob.vectors` rows hold 1536-dim embeddings derived from customer content. Embeddings are reversible enough to leak content under inversion attacks.
- **Mitigation:** treat as PII for residency, retention, and right-to-erasure. Add a `purge by source_ref` admin endpoint and document the data classification.

### T-RL-1 — AOAI tier rate-limit DoS amplification
- **Where:** S0 tier 429s today on a single 25-page attachment (we observed this). Concurrent pipelines × pages × retries can starve other pipelines.
- **Mitigation:** per-pipeline embed concurrency cap (e.g., 4); exponential backoff with jitter; circuit-breaker per AOAI deployment.

## 6. Mitigations checklist (what to do next)

- [x] **High** Migration script `scripts/scrub_model_api_keys.py` clears legacy `api_key` from `docgrok_model` records (push to Key Vault first, fall back to `--force-clear` once AAD verified). *(T-MET-1, batch 1.)*
- [ ] **High** Replace `OMNIVEC_ADMIN_TOKEN` with AAD bearer + role gating. Add `audit_log` table for admin operations.
- [x] **High** `attachment_blob_account_allowlist` config + mandatory pin: absolute attachment URLs are rejected unless host matches `account_url` or the allowlist. *(T-CON-2, batch 1.)*
- [ ] **Medium** Sandbox `pipeline-worker` parser in a subprocess with `RLIMIT_AS=1GB` and seccomp; cap pages-per-attachment to 200.
- [ ] **Medium** Isolate change-feed lease container into its own Cosmos DB with a separate SA.
- [x] **Medium** Per-AOAI-deployment embed semaphore + jittered exponential backoff in pipeline-worker (`OMNIVEC_EMBED_CONCURRENCY`, default 4). *(T-RL-1, batch 1.)*
- [x] **Low** Attachment IDs / blob keys validated: traversal segments (`..`, `.`), control chars, leading slashes, and empty segments are rejected; absolute-URL paths are sanitized after URL-decoding. *(T-BLB-1, batch 1.)*
- [ ] **Low** Document data classification of `e2eblob.vectors` as PII; add purge-by-source endpoint.
- [ ] **Low** Add CSP + rate-limit at omnivec-web ingress.

## 7. How to update this model

1. Edit this file. Diff is the source of truth.
2. Update the Mermaid DFD if the architecture changes.
3. Re-run STRIDE table when adding a process / data store / external interactor.
4. Add new project-specific risks under section 5 with a `T-XXX-N` id.
5. Mark items in section 6 done as PRs land.

## 8. Out-of-scope for this iteration

- Threat model of CI/CD pipeline (GitHub Actions → ACR push). Tracked separately.
- Threat model of Helm chart / Terraform infra. Tracked under `infra/`.
- Customer-side hardening of source CosmosDB / Blob. We document the
  contract; customers are responsible for their own perimeter.
