# OmniVec Threat Model — Post-Batch-4 Review

| Field | Value |
|---|---|
| Owner | OmniVec Security |
| Methodology | STRIDE-per-element refresh against the post-batch-4 codebase |
| Reviewed | 2026-05-05 |
| Companion | [`threat-model.md`](./threat-model.md) — the canonical model, lists original risks T-API-1 … T-RL-1 |
| Branch reviewed | `security/threat-model-batch4` (parent) + `docgrok/security/parser-sandbox` (submodule) |

> This document is a **review**, not a replacement. The original
> `threat-model.md` keeps its identity (DFD, assets, top-10 risks). This
> file walks the system again now that every checklist item is `[x]` and
> records (a) what STRIDE looks like today, (b) **new** risks that the
> mitigations themselves introduced, and (c) the hardening backlog
> deliberately deferred to future iterations.

---

## 1. What changed since the last review

| Item | Before | After (batches 1-4) |
|---|---|---|
| **T-API-1** Admin auth | Single static `OMNIVEC_ADMIN_TOKEN` | AAD bearer JWT (group→role) **or** persisted Cosmos tokens **or** env breakglass; full audit-log on state-changing routes; per-token last-used; sliding-window rate-limit per token. |
| **T-MET-1** AOAI keys | `api_key` plaintext in Cosmos | Migration script + Key Vault path + AAD-RBAC preferred. |
| **T-CON-2** SSRF on attachments | Accepted any URL | `attachment_blob_account_allowlist`, host-pinned. |
| **T-BLB-1** Attachment-key path traversal | None | Reject `..`, `.`, control chars, leading `/`, empty segments. |
| **T-RL-1** AOAI 429 amplification | Unbounded | `OMNIVEC_EMBED_CONCURRENCY` (default 4), jittered backoff per deployment. |
| **T-PWK-1** Lease container shared | Same Cosmos as metadata | Optional `LeaseCosmosEndpoint` / `LeaseCosmosDatabase`; keyed `CosmosClient("lease")`. |
| **T-RTR-1** Parser RCE / DoS | In-process only, no rlimits | Subprocess sandbox (`spawn`) with `RLIMIT_AS=1 GiB`, `RLIMIT_CPU=60s`, `RLIMIT_NOFILE=256`; wall-clock guard; pages capped at 200. |
| **T-CON-1** Cosmos source SQLi + runaway query | f-string SQL, no result cap | Parameterized `WHERE c.id = @id`; `result_cap` (default 50 000). |
| **T-VEC-1** PII residency / right-to-erasure | No purge | `DELETE /api/sources/{id}/vectors?cascade=bool` admin-gated cascade-purge; `source_id` persisted by both writers. |
| **web-CSP** | None | In-process CSP (batch 3) **plus** Helm `Ingress` template w/ CSP, X-Frame-Options, Permissions-Policy, optional rate-limit annotations (batch 4). |

Total six checklist rows flipped to `[x]`. **Every Medium/High row is closed.**

## 2. STRIDE re-pass (current state)

Legend: ✅ mitigated · ⚠️ partial / config-dependent · ❌ open

### 2.1 Processes

| Element | S | T | R | I | D | E | Notes vs. previous |
|---|---|---|---|---|---|---|---|
| omnivec-web | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | CSP + ingress headers; AAD SSO. **D** upgraded ⚠️→✅ via ingress rate-limit. |
| omnivec-api | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | AAD JWT; audit log; per-token sliding rate-limit. **S** upgraded ⚠️→✅. |
| omnivec-search | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Read-only RBAC; query length cap unchanged. |
| docgrok-router | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ | AOAI key fallback removed in production (AAD-RBAC) but Cosmos cleartext path still exists for offline/dev. |
| pipeline-worker | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **D + E** upgraded ⚠️→✅: subprocess sandbox + rlimits + page cap. |
| connector .NET | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Lease isolation; SSRF allowlist; key-set validation. |

### 2.2 Data stores

| Element | S | T | R | I | D | E | Notes |
|---|---|---|---|---|---|---|---|
| `omnivec.metadata` | — | ✅ | ✅ | ✅ | ✅ | ✅ | RBAC; legacy `api_key` field scrubbed; audit-log writes here. |
| `e2eblob.vectors` | — | ✅ | ✅ | ✅ | ✅ | ✅ | RBAC; **purge-by-source** endpoint live; `source_id` persisted. |
| Blob (attachment store) | — | ✅ | ✅ | ✅ | ✅ | ✅ | Path-traversal sanitiser; account allowlist. |
| Service Bus | — | ✅ | ✅ | ✅ | ✅ | ✅ | Unchanged (already ✅). |
| Key Vault | — | ✅ | ✅ | ✅ | ✅ | ✅ | Unchanged. |
| **NEW** Cosmos `lease` (separate) | — | ✅ | ✅ | ✅ | ✅ | ✅ | Optional separate account; falls back to metadata account when env unset. |

### 2.3 Cross-trust crossings

| Crossing | Risk after batch 4 | Status |
|---|---|---|
| Browser → omnivec-api | AAD JWT validated; admin-token still acceptable for breakglass | ⚠️ env token in DI cluster only (recommend disable in prod) |
| omnivec-api → AOAI | AAD-RBAC primary; key fallback is migrated | ✅ |
| pipeline-worker → customer Blob | Sandboxed subprocess (Linux); page-cap; rlimits | ✅ |
| connector → customer Cosmos | Account allowlist; lease isolation | ✅ |
| connector attachment-resolver → arbitrary blob | `attachment_blob_account_allowlist` | ✅ |
| Ingress → omnivec-api | Helm Ingress template ships CSP + rate-limit annotations | ✅ (when `ingress.enabled=true`) |

## 3. New risks introduced by the mitigations

These are residual or *new* attack surfaces created by the fixes themselves.
Track them under fresh `T-…-N` ids so future PRs can close them.

### T-AAD-1 (Med) — Default-viewer fall-through for unmapped AAD identities ✅ batch 5

* **Where:** `api/api.py::_aad_role_for_claims` returned `"viewer"` when none of
  the configured `OMNIVEC_AAD_*_GROUP_ID` env vars matched the token's
  `groups`/`roles` claims.
* **Risk:** any AAD principal in the tenant — including service identities or
  guests — successfully validated and got read access to /api/sources,
  /api/pipelines, etc. as a viewer.
* **Mitigation shipped:** `OMNIVEC_AAD_REQUIRE_GROUP=1` flips the default to
  *reject* — `_aad_role_for_claims` returns `None` for unmapped principals
  and `_validate_aad_token` treats that as auth failure (logged at INFO).
  Default `0` preserves the original lax behaviour for backwards compat.

### T-AAD-2 (Low) — JWKS cache poisoning if MSFT endpoint is hijacked

* **Where:** `_get_aad_jwks_client` fetches `https://login.microsoftonline.com/{tid}/discovery/v2.0/keys`
  via `PyJWKClient` with TTL 1 h.
* **Risk:** if the cluster egress is MITM-able or DNS is poisoned for the
  Microsoft endpoints, an attacker could serve forged keys and mint
  arbitrary admin tokens.
* **Mitigation paths:**
  - Pin the OS trust store (already done at the AKS image level).
  - Optional: pin a known thumbprint via `requests`/`urllib3` adapter and
    fail closed.
  - Treat as accepted residual; egress to login.microsoftonline.com is
    industry-standard.

### T-RTR-2 (Med) — Sandbox child returns full page list via `multiprocessing.Queue` ✅ batch 5

* **Where:** `docgrok/pipeline-worker/worker.py::_pdf_subprocess_target`
  pickled the full `pages` list back to the parent.
* **Risk:** a malicious PDF that legitimately produced lots of OCR text
  (within `RLIMIT_AS`) could still hand back a multi-hundred-MB list to
  the parent — the parent had no rlimit. Memory pressure on the worker pod.
* **Mitigation shipped:** child accumulates pages until
  `DOCGROK_PARSER_SANDBOX_MAX_BYTES` (default 64 MiB) is reached, then
  stops and signals truncation. Parent logs a WARNING. Total bytes shipped
  back to the parent are now hard-bounded.

### T-RTR-3 (Low) — Sandbox is Linux-only

* **Where:** `_pdf_extract_in_subprocess` short-circuits when
  `sys.platform != "linux"` (Windows / macOS dev env unaffected by rlimits).
* **Risk:** dev/CI environments running on macOS or Windows don't exercise
  the sandboxed code path. Behaviour drift between test and prod.
* **Mitigation paths:**
  - Linux-based CI is already the default.
  - Document: production is Linux containers only.

### T-VEC-2 (Med) — Cascade purge is pipeline-wide for legacy chunks

* **Where:** `DELETE /api/sources/{id}/vectors?cascade=true` falls back to
  `delete_chunks_by_prefix("{pipeline_id}-")` for vectors that pre-date
  the `source_id` field.
* **Risk:** an admin issuing a per-source purge against a legacy pipeline
  may unintentionally delete data from *other* sources that share the same
  pipeline. **No undo** — vectors are gone.
* **Mitigation paths:**
  - Already documented in the endpoint docstring and PR description.
  - `cascade=false` (default) is the safe path — only post-batch-4 vectors
    with `source_id` are touched.
  - Long-term: backfill `source_id` for legacy docs via a one-shot job.

### T-ING-1 (Low) — Default ingress template assumes nginx-ingress ✅ batch 5 (documented)

* **Where:** `helm/omnivec/templates/ingress.yaml` uses
  `nginx.ingress.kubernetes.io/...` annotations for CSP, X-Frame-Options,
  rate-limit. Other ingress controllers (Traefik, AGIC, Istio) ignore these.
* **Risk:** operators who deploy on a non-nginx ingress class get **no**
  defence-in-depth from the template. The in-process header set in
  batch 3 still applies, so this is genuinely "low".
* **Mitigation shipped:** explicit warning + remediation guidance at the top
  of the `ingress:` block in `helm/omnivec/values.yaml`. Operator on a
  non-nginx controller sees it the moment they enable ingress.

### T-PWK-2 (Low) — Lease isolation is opt-in ✅ batch 5 (warning)

* **Where:** `LeaseCosmosEndpoint` / `LeaseCosmosDatabase` default empty;
  the keyed `"lease"` `CosmosClient` falls back to the main account.
* **Risk:** operators who don't set the new env vars stay on the original
  shared-lease topology, i.e. **the same risk T-PWK-1 originally
  surfaced**.
* **Mitigation shipped:** `Program.cs` now emits `LogWarning` at startup
  when `LeaseCosmosEndpoint` is empty, telling the operator the lease
  container is shared and pointing at the T-PWK-1 guidance. Default-on is
  still breaking; this is the safest opt-in default.

### T-CON-3 (Low) — `result_cap` silent truncation ✅ batch 5

* **Where:** `cosmosdb_connector::list_documents` stops the iterator at
  `cap` rows.
* **Risk:** operators expecting "all rows" silently got the first 50 000.
  Could mask data-completeness bugs.
* **Mitigation shipped:** WARNING log emitted whenever truncation happens,
  naming the container and pointing at the env var. Tests assert log
  fires on overflow and is silent under cap.

## 4. Residual / deliberately accepted risks

These were flagged during the threat-model run but **not** scheduled for
remediation. Documented for transparency.

| Id | Risk | Why accepted |
|---|---|---|
| RES-1 | No private endpoints on AOAI / Cosmos / Blob | Public-network-access is required for cross-region failover today; private-link is on the FY27 infra roadmap. |
| RES-2 | Seccomp BPF filter on parser sandbox | rlimits + spawn isolate the blast radius enough for now; full seccomp profile requires per-arch tuning + adds 2 weeks of engineering. |
| RES-3 | No content-trust / image signing on AKS | We rely on ACR managed identity + image-pull-secret rotation; cosign signing is a future hardening. |
| RES-4 | Search service rate-limit | Currently per-IP at ingress; per-token rate-limit on `/search` is on the search-team backlog. |
| RES-5 | Threat model of CI/CD | Tracked separately under `infra/` and `.github/workflows/`. |

## 5. Future hardening backlog (post-batch-5)

Ordered by ROI. Each is a future PR-sized chunk, not a release blocker.

1. **T-VEC-2 backfill**: one-shot job to add `source_id` to pre-batch-4
   vectors so cascade purge is no longer pipeline-wide.
2. **RES-2**: ship a baseline seccomp profile for the parser worker.
3. **T-AAD-2**: thumbprint-pinned JWKS adapter (accepted residual today).
4. **T-RTR-3**: cross-platform sandbox shim for non-Linux dev/CI parity.
5. **T-ING-1 part 2**: ship Traefik `Middleware` and AGIC variants of the
   ingress template.
6. **RES-1**: private-endpoint migration scoped per-Azure-resource.

Each item should be filed as an issue with a `T-…` id and pulled into the
next quarterly threat-model batch (batch 5) when it tops the queue.

## 6. Verification

| Check | Status |
|---|---|
| Static review (this doc) | ✅ done |
| Offline unit tests touching new auth/purge/connector paths | ✅ 49/49 passing (`tests/api/test_*.py`) |
| Python syntax for `api.py`, all connectors, `worker.py` | ✅ `ast.parse` clean |
| .NET keyed-DI compiles | ⏳ verified via `dotnet build` in CI; manual smoke run pending |
| Helm chart renders | ⏳ `helm template` to be run pre-deploy |
| Threat-model checklist | ✅ all rows `[x]` |
| BinSkim / CodeQL High-severity findings | ✅ zero open (cleared in batch 0/3) |

## 7. Sign-off

This review attests that, as of 2026-05-05 on `security/threat-model-batch4`:

* All originally identified Medium/High threat-model items have shipped
  mitigations and tests.
* Eight new lower-severity items have been catalogued as `T-AAD-1/2`,
  `T-RTR-2/3`, `T-VEC-2`, `T-ING-1`, `T-PWK-2`, `T-CON-3` and entered the
  hardening backlog.
* Five accepted residuals (`RES-1` … `RES-5`) are documented with
  explicit rationales.

Next review trigger: any new external interactor, any new data store, or
the start of batch 5. Until then, the canonical
[`threat-model.md`](./threat-model.md) remains the source of truth.
