# Separate bounded, real-agent recovery scenario (PR183 only)

**Preparation is authorized; LIVE EXECUTION REMAINS HELD until the coordinator
explicitly authorizes this scenario.** This is separate from the worker/restart
chaos harness. It neither edits that harness nor calls its outage/restore methods.
It reuses only its allowlist and read-only original-data/model baseline checks.

## Safe commands now

```powershell
python scripts\agent-recovery-scenario.py
python -m pytest tests\unit\test_agent_recovery_scenario.py -q
```

Default mode makes **zero external calls**. The standalone suite currently has
**89 passing offline cases**. Combined regression validation:

```powershell
python -m pytest tests\unit\test_recovery_chaos.py tests\unit\test_recovery_chaos_probes.py tests\unit\test_agent_recovery_scenario.py -q
```

No live fixture, new source/destination/pipeline, chat request, approval, replica
change or rollout is performed by these commands.

## Exact future test actions

1. Validate the existing PR183 AKS allowlist and kubeconfig server; establish the
   read-only four-TXT-document, model/route, real-embedding and semantic-search
   baseline. Require workers=2, API=2, Blob ingestor=1, router/controller=1,
   agent=1 and established Cosmos changefeed=15 to be ready.
2. Reject a running base-chaos lock. Atomically create only
   `pr183-agent-recovery-lock`, recording a random unique run prefix. Both suites
   must still be serialized by the coordinator: the base harness is unchanged
   and does not know this new lock.
3. Execute the maintained pod probe inside a ready API pod. Read
   `OMNIVEC_ADMIN_TOKEN` **only inside that pod**. Send that existing authenticated
   admin bearer only to `http://127.0.0.1:8080/api/...`.
   Verify `/api/agent/tools` returns the authenticated admin catalog, including
   read-only `diagnose_pipeline` and approval-gated `resume_pipeline`.
4. Require existing chat model **`mdl-ext-075dd3b9`** and protected 1536-dimensional
   embedding model **`mdl-ext-52423101`**. Do not create/edit/delete models,
   credentials, roles, federations, Kubernetes workloads or Azure deployments.
5. In **`omnivec-test-v3fapuy5j6s7c` / `testdb` only**, create NEW source/vector
   containers under `pr183-agent-recovery-<32-hex-run-id>`. Register exactly one
   new Cosmos source, vector destination and queue/chunk pipeline. Refuse existing
   names/containers; never adopt previous fixtures. The initial source is empty.
6. Pause only the newly returned pipeline ID and verify its exact name,
   source/destination/model binding. Wait 45 seconds for quiescence; insert
   **one new owned source document**, bounded to 2–20 chunks using the same
   `chunker.chunk_text` strategy/configuration as `e2e-cosmos-chunking.py`.
   After 15 seconds require paused status and **zero actual destination rows**.
7. Wait up to 360 seconds for naturally refreshed, healthy cached checks for
   these two new dependencies and the protected model. Never trigger broad
   `/api/health/checks/run` across originals.
8. Make one real `POST /api/agent/chat`:

   ```json
   {
     "model_id": "mdl-ext-075dd3b9",
     "messages": [{"role": "user", "content": "Diagnose only the exact new pipeline; propose one scoped resume if pause is the only fault."}]
   }
   ```

   The actual prompt supplies the new pipeline ID and explicitly disallows all
   other mutations. Parse the implemented SSE `data: {JSON}` protocol with a
   deadline, byte/event caps and mandatory `done`; errors/truncated streams fail.
   Never trust natural-language claims.
9. Require a preceding **real scoped `diagnose_pipeline` tool result**: only
   `paused` findings, `BLOCKED`, and no unknown dependencies. Require exactly one
   `approval_required` event for `resume_pipeline`, with args **exactly**
   `{"pipeline_id": "<new-owned-id>"}`—no additional fields.
10. Re-read the legitimate server-side pending approval through
    `GET /api/agent/sessions/{session_id}/approvals`. Require exactly one pending
    call matching session/call/tool/args. Reconfirm paused status and zero rows.
11. Submit **one** authenticated `POST /api/agent/chat/approve`:

    ```json
    {
      "session_id": "<session returned by real chat>",
      "call_id": "<revalidated pending call>",
      "decision": "approve",
      "comment": "Parent-authorized isolated test: resume only the exact new synthetic pipeline."
    }
    ```

    No client-supplied `X-Caller-Id`, `X-Caller-Role`, internal agent bearer or
    private-header impersonation is used. The existing authenticated API proxy
    derives trusted upstream identity. No direct `/v1/...` calls or invented
    diagnostic/verification proxy endpoints are needed.
12. Require the approved call's real `approval_decision`, matching
    `resume_pipeline` `tool_result`, runtime `verification` event and authoritative
    final recovery record. Accept **only `VERIFIED_PROCESSING`**, attempt=1,
    exact owned pipeline, healthy two-observation evidence, fresh ordered
    timestamps and destination count increase from zero. Another proposed action
    is **not approved** and fails this one-action scenario.
13. Independently query the **actual new Cosmos destination**, not merely API
    readiness or agent counters. Require exact expected chunk count/content,
    source/pipeline/reference identities, contiguous indices, no duplicate IDs,
    finite nonzero 1536-dimensional distinct vectors, unchanged source content
    and increased API destination progress. Observe chunks twice, five seconds
    apart. Only then label repair actor `approved_real_agent_resume_pipeline`.
14. In `finally`, pause **only the new pipeline**, validate its ownership/bindings
    again, and verify paused state twice. **Never manually resume/run it**, even
    to obtain a passing result. Retain synthetic containers, document,
    registrations and audit/session evidence; do not delete checkpoints/queues.
15. Recheck original TXT vector fingerprints/model/routes and established replica
    readiness without modifying them. Release only this run's lock after verified
    cleanup (or a proven pre-fixture block) and original baseline preservation.
    Write the structured report even for blocked/failed outcomes.

## Live prerequisites / explicit command

Required before coordinator clearance:

- Priority-1 rollout complete and original baselines stable; no concurrent chaos,
  fixture mutation, deployment or scaling by another operator.
- Final agent image deployed; real approved GPT-4o chat/model-secret configuration
  functioning. Registration alone is not a provider-success proof.
- Dedicated agent identity federation and Service Bus management read permissions
  fixed. `AADSTS700213`, unknown queue counters, observed DLQ blockers or any other
  non-pause diagnosis block approval. Do **not** purge queues to manufacture health.
- Agent API authentication permits the existing approved `resume_pipeline` call.
  This API-level action does not require Kubernetes mutation permissions; do not
  enable unrelated scale/restart/remediation permissions for this scenario.
- API pod workload identity can create/query the new containers in the **separate
  test data account**, and the normal controller refreshes cached health for new
  source/destination registrations within the observation budget.
- Normal queue/changefeed/worker processing works. Only the new synthetic pipeline
  is paused; originals and fixed changefeed=15 / workers=2 are untouched.

**Only after explicit authorization for this separate scenario:**

```powershell
python scripts\agent-recovery-scenario.py `
  --kubeconfig C:\Users\prsasatt\.kube\omnivec-omnivec-pr183-test `
  --execute --confirm ISOLATED-PR183-AGENT-RESUME-AUTHORIZED `
  --report agent-recovery-live-UNIQUE.json
```

The base harness's confirmation phrase is intentionally insufficient. Reports
must be new files inside the current worktree. Exit 0 requires a full pass;
nonzero means blocked/failed execution or invalid authorization.

## Bounds, evidence and unresolved limitations

- One new document, 2–20 chunks, one chat/proposal request and at most **one
  approved resume**; no repeated approvals or destructive fallback. Each agent
  turn retains the agent runtime's own iteration limits.
- Pod scenario deadline=1,200 seconds, separate cleanup deadline=180 seconds,
  host exec timeout=1,500 seconds. Linux pod alarms also bound SDK/SSE blocking.
  Each SSE stream is limited to 240 seconds between checks, 512 events / 2 MB;
  socket reads are bounded at 180 seconds. Raw model text, SSE/HTTP bodies and
  exception details are not persisted in reports.
- The deployed agent's runtime currently samples at most twice, five seconds
  apart, after its action (each observation itself is bounded). A slow changefeed
  may process outside that window. **Later chunks cannot upgrade a runtime
  `READY_IDLE` / `NOT_VERIFIED` outcome into a pass.** Report this limitation and
  obtain an evidence-based follow-up from the coordinator; never manually resume
  or reapprove to manufacture a favorable window.
- An approval transport failure is ambiguous: do not retry it. If no action result
  was observed, allow 90 seconds from approval submission for the documented
  35-second pre-action snapshot / 30-second HTTP action bounds, then perform final
  owned pause and verify it. Report ambiguity, not repair. Extreme scheduling
  delays or API pod loss can still prevent conclusive pause verification or allow
  remote execution to outlive the host; retain the lock and inspect the exact
  recorded prefix/session/pipeline before any manual action. There is no fabricated
  cancellation endpoint or claim of crash-proof cleanup.
- `repair_actor`, approved runtime outcome, actual `processing_recovery`, original
  preservation and cleanup are separate fields. Harness final pause is cleanup,
  not repair. Read-only `verify_recovery`, snapshot health, `READY_IDLE`, GPT prose,
  model registration or pod readiness are not approved-agent repair evidence.
- No Cosmos chunk revision race is exercised or claimed fixed. This is a single
  new document, not concurrent update/delete or replay testing.

Offline cases verify proposal and pending-record scope, legitimate proxy payloads,
unsafe-tool rejection, missing/unknown diagnostics, all non-success runtime
outcomes, stale/unchanged/foreign progress, LLM-prose false positives, actual chunk
integrity, no manual-resume path, no retry after ambiguous approval, finally pause
on failures, ownership/cleanup failures and zero-call default planning.
