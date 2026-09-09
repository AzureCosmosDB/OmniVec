# Isolated PR183 recovery chaos

**Do not execute while another rollout/test is active. Parent/coordinator clearance
is required. Building and running offline tests does not authorize a live run.**

This maintained harness ports the previously successful bounded Blob backlog test.
It does not deploy images or implement a troubleshooting agent. Default plan mode
makes **zero external calls**; the live mode is deliberately allowlisted to
subscription `074d02eb-4d74-486a-b299-b262264d1536`,
`rg-omnivec-omnivec-pr183-test`, AKS `omnivec-aks-v3fapuy5j6s7c`,
namespace `omnivec`. Configuration has no credentials. Namespace, kubeconfig,
source and destination are explicit/configurable, but changing protected resource
IDs requires a reviewed allowlist change, not merely a confirmation flag.

## Offline validation (safe now)

From the PR183 worktree:

```powershell
python scripts\recovery-chaos.py
python -m pytest tests\unit\test_recovery_chaos.py tests\unit\test_recovery_chaos_probes.py -q
```

Tests mock all external execution. Cases include unavailable dependencies,
timeouts/redacted errors, a competing lock, unready watchdog, lost host/arm
response, transient watchdog failure, failed restoration, assertion-failure
cleanup, invalid vectors and duplicate identities.

### Repeatable offline suite matrix

The expanded suites currently contain **161 passing offline cases**. These are
fault-injection/contract tests, **not 161 live outages** or evidence that an agent
repaired anything. They use the existing pytest runner, fake Azure/Kubernetes/HTTP
objects, and deterministic clocks; no cluster or network mutations are performed.

| Suite | Cases | Specific verification |
| --- | ---: | --- |
| `test_recovery_chaos.py` | 107 | Default zero-call plan, explicit authorization/report bounds, AKS/FQDN/TLS allowlist, HPA/unhealthy/stale-signal rejection, atomic concurrent lock, watchdog arm-before-outage ordering, exact scoped restarts only after owned queue evidence, timeout/finally restoration, actual replica/generation convergence, watchdog acknowledgement before deletion, host loss/transient or persistent dependency outage, missing EventGrid cleanup retaining lock, manual/watchdog attribution, resourceVersion-protected owned annotation cleanup, separate primary/restoration/cleanup stages and return codes, credential-safe limited diagnostics, strictly bounded read-only transport retries, stdin script delivery and no mutation/exec retry |
| `test_recovery_chaos_probes.py` | 54 | Dimension/nonfinite/zero/Boolean embedding rejection, lost/foreign/duplicate documents, original ID/content/vector drift, delayed duplicate detection, two persisted recovery observations, overwrite refusal and run ownership, ETag/ownership cleanup races, missing deletion events, peek-only source/pipeline/reference matching, malformed/non-object foreign messages, bounded 1,000-message scanning without consuming/purging, unavailable Service Bus, model/route/source/destination drift, real embedding invocation, wrong/empty semantic results, loopback-only admin auth, redacted HTTP/timeout errors |

Repeat both suites together to catch accidental cross-suite interactions. For
focused investigation, use the existing pytest selectors (still offline):

```powershell
# Outage orchestration and independent restoration
python -m pytest tests\unit\test_recovery_chaos.py -q -k "watchdog or restore or injection"
# Processing/data correctness beyond pod readiness
python -m pytest tests\unit\test_recovery_chaos_probes.py -q -k "embedding or recovery or registry or semantic"
# Only owned messages/Blobs, unavailable dependencies and deletion delivery
python -m pytest tests\unit\test_recovery_chaos_probes.py -q -k "queue or servicebus or cleanup or upload"
```

The tests exposed and fixed two harness-probe validation gaps: JSON Boolean
arrays are not real embedding vectors, and valid non-object JSON messages in an
unrelated backlog must be skipped rather than mistaken for owned message objects.
No shared ingestion, API or agent behavior was changed by those fixes.

## Live prerequisites and authorization

- Coordinator has completed and serialized UI/CLI/API/agent rollouts; no other
  test mutates these pipelines or originals. The cluster lock only coordinates
  this harness, not unrelated operators or deployment systems.
- `python`, authenticated `az`, `kubectl`, and the explicit kubeconfig exist.
  Azure identity is queried with the exact subscription; the harness does not
  switch account context. Kubeconfig server must match AKS FQDN with TLS enabled.
- Workers=2, API=2, Blob ingestor=1, router=1, router controller=1, all observed
  generations/updated/ready/available replicas converged. Targeted HPAs block
  execution; external GitOps/KEDA scaling must also be quiescent.
- TXT pipeline `pip-11f0ffed`, source `src-1b2aac6d`, destination `dst-ef1231b2`
  remain active and correctly scoped; exact four baseline documents include
  `chaos-backlog.txt`. Existing rows must carry the configured source ID.
- Original model `mdl-ext-52423101` and routes `trp-34a1c7fa` /
  `trp-e0728ae8` produce real finite nonzero 1536-dimensional embeddings. Three
  original semantic queries must have the expected top result.
- API image contains Python, Azure Cosmos/Blob/Service Bus/Identity SDKs and
  Kubernetes SDK. API pod identity can read scoped storage, upload/delete one
  owned Blob, and **peek** the embeddings/worker subscription. BlobDeleted
  EventGrid delivery and the enabled consumer must work.
- Operator can create/read/replace/delete the named ConfigMap lock and create,
  observe and delete its uniquely named Job; scale only the worker deployment;
  restart the four explicitly named deployments; exec into an API pod.
- Watchdog uses the API deployment's service account and image, with no admin
  token/environment copied. It must read/patch the worker deployment. The API
  service account does not need ConfigMap access: the host mirrors the run/phase
  into two owned deployment annotations (not pod-template annotations, so no
  extra rollout). Existing stale annotations block preflight. The Job verifies
  admission/RBAC via server-side **dry-run patch**
  and emits readiness **before any outage**. Missing permission blocks the test;
  this harness never grants RBAC.
- Reserve a new report filename in the current worktree. Raw command/HTTP/SDK
  responses and credentials are never printed or saved. API admin authentication
  is read from `os.environ["OMNIVEC_ADMIN_TOKEN"]` only inside the API pod and sent
  only to `127.0.0.1:8080`, never the public HTTP UI.

**Only after the coordinator explicitly authorizes live disruption:**

```powershell
python scripts\recovery-chaos.py `
  --config scripts\recovery-chaos-pr183.json `
  --kubeconfig C:\Users\prsasatt\.kube\omnivec-omnivec-pr183-test `
  --namespace omnivec --source-id src-1b2aac6d --destination-id dst-ef1231b2 `
  --execute --confirm ISOLATED-PR183-CHAOS-AUTHORIZED `
  --report recovery-chaos-live-UNIQUE.json
```

Exit 0 means all implemented checks passed; 1 means blocked/failed execution;
2 means invocation/allowlist authorization was rejected. Read the structured
report, not just the exit code.

## Safety and evidence

1. Read-only healthy/model/data/search preflight, then atomic fixed-name
   ConfigMap `create` lock, then recheck baseline. No stale-lock auto-expiry or
   deletion: inspect an interrupted run before explicitly releasing its lock.
2. Capture original deployment replicas in report and lock; create independent
   Kubernetes watchdog Job and verify its readiness/RBAC. Only then arm and
   scale workers to zero.
3. Create one uniquely named `pr183-chaos-<run-id>.txt` with `overwrite=False` and
   ownership metadata. **Never overwrite the original `chaos-backlog.txt`.**
4. Peek (never consume/settle) at most 1,000 messages per observation to find
   only this synthetic source/pipeline/reference. Confirm no persisted fixture
   while workers are actually zero. A backlog beyond this observation bound
   fails rather than purging or consuming anything.
5. Request router, router-controller, API and Blob-ingestor rolling restarts.
   Worker restoration runs in `finally`, including assertion failure. The
   independent watchdog attempts restoration after at most 180 seconds from
   watchdog readiness, or after 120 seconds if the arm signal is lost. It retries bounded
   Kubernetes calls for 600 seconds; Job hard deadline is 1,200 seconds.
6. Verify actual original replicas and rollout generations, mark lock/signal restored,
   wait for watchdog completion/acknowledgment, recheck health, **then** delete
   only that Job. On unverified restoration keep watchdog and lock and fail.
   A prolonged control-plane outage cannot guarantee the 180-second recovery;
   this is a recovery-attempt bound, not an availability SLA.
7. Verify fixture processing, exact originals' IDs/content hashes/vector
   fingerprints, finite embeddings, model/route IDs, zero duplicate small-doc
   chunks and real top-result search. Delete only the ETag/metadata-verified
   current-run Blob; wait for normal EventGrid vector removal and exact original
   baseline, then remove only this run's signal annotations and lock. Cleanup
   failure retains the lock and records failure. No vector,
   checkpoint, original model, original Blob, queue or DLQ is ever deleted.

Normal work is bounded at 1,500 seconds, with a separate 420-second restoration
budget and 480-second processing/cleanup budget. Individual external processes
and SDK calls are bounded; failed execs are not retried because writes may have
committed. Hard host termination can leave the owned fixture/report incomplete;
the independent watchdog restores worker capacity but intentionally does not
delete data or unlock the run. Inspect the run ID, Job logs, lock and all actual
deployment states before targeted manual cleanup. Do not broadly delete
`pr183-*` resources.

Reports separate `fault_injected`, `observed_failure`, `repair_actor`,
`readiness`, `processing_recovery`, `data_model_identity_and_duplicates`,
`cleanup`, and `result` (`blocked`/`failed`/`passed`). A manual harness restore is
**not** autonomous-agent repair. The watchdog is reported separately when its
restore attempt is observed. The base report labels conversational repair
`not_exercised_by_base_harness` and diagnostics
`not_integrated_by_base_harness`. These describe **this suite's coverage**, not
the deployment's available chat model or implemented diagnostic contracts. Use
the separately authorized agent-recovery scenario for actual approved-agent proof.

### Failure evidence and bounded read-only retries

The report preserves `primary_failure`, `restoration_failure`, `cleanup_failure`
and `lock_cleanup_failure` independently when applicable. Each retains its
original stage/operation, exception type, return code (null when unavailable)
and error classification; a later cleanup operation cannot overwrite the primary
cause. `last_operation` remains informational, not the only failure evidence.

Command diagnostics retain a **limited safe projection of stderr**: at most four
recognized fixed phrases / 384 characters, derived from at most its final 32 KiB.
Examples include `i/o timeout`, `unauthorized`, `TLS certificate validation failed`
and `Kubernetes backend transport failure`. Arbitrary stderr text, URLs, addresses,
quoted values, credentials and raw stdout are never copied. Unknown stderr is
explicitly marked omitted, rather than treated as a known root cause. The pod's
allowlisted `CHAOS_ERROR=<exception type>` marker is retained separately if present.
Process timeouts preserve `process_timed_out=true` and any safe diagnostic phrases.

Only explicitly allowlisted **built-in `kubectl get` resource reads/lists** may
retry, and only for classified transport failures/timeouts. There are at most
three attempts, each at most ten seconds, with one-/two-second backoffs included
in the original operation timeout and remaining harness deadline. Failed attempts
are recorded in `read_attempt_failures`, including whether another attempt was
actually scheduled. Successful recovery does not erase the earlier read evidence.

There is **no automatic retry** for exec (even a read-only pod probe), scale,
restart, create, patch, replace, delete, apply, arbitrary plugins, raw URLs, watch
streams, commands with stdin documents, JSON parse errors, authorization/TLS
validation failures, missing resources, throttling or unknown failures. In
particular, an ambiguous write/approval/exec response is never automatically
reissued. Authentication/authorization evidence also prevents retries when a
process subsequently times out. These refinements do not authorize a live rerun
or transfer retained-fixture cleanup ownership away from the coordinator.

Pod scripts are now sent through `kubectl exec -i ... -- python3 -` stdin;
the Python file is **not** embedded in a Windows `-c` command-line argument.
Only the action and bounded credential-free configuration/baseline payload remain
in argv. All seven probe actions retain the same protocol and deadline, with no
exec retry. This reduces command-line size/quoting exposure; it is not proof that
argument length caused the original failed run. Coordinator-confirmed targeted
cleanup of that run remains a separate manual follow-up, not a retroactive pass.

## Explicitly excluded / optional follow-up

No network denial, model deletion, production/DLQ replay or queue purge is
implemented. Duplicate/replay injection is not claimed; only persisted duplicate
detection is covered. Deterministic diagnostics/recovery tools are not invoked
by this base harness; their deployed availability must not be inferred from its
coverage labels.

Multi-chunk update/obsolete-vector cleanup is a **separate, serialized opt-in** follow-up using
the maintained `scripts\e2e-cosmos-chunking.py`, not silently part of this
harness's result. That script accepts only the separate
`https://omnivec-test-v3fapuy5j6s7c.documents.azure.com:443/` data account,
`testdb`, existing model, and a unique `pr183-chunks-*` prefix. It creates/retains
synthetic registrations/containers and pauses its pipelines on exit; inspect its
current behavior and obtain separate coordinator clearance before pod execution.
Never target metadata storage or reactivate old paused fixtures for convenience.
The maintained Cosmos probe currently checks shrink and empty-content vector
cleanup, not physical Cosmos source deletion. Physical multi-chunk Blob deletion
was exercised by the earlier session probe, but is not implemented or claimed
by this maintained harness; its automatic Blob fixture is deliberately one chunk.

**Known limitation:** out-of-order/concurrent Cosmos chunk replacement is
non-atomic and not revision-fenced. Normal sequential bounded chunk tests do not
prove race safety and this harness neither injects that unsafe race nor claims
it fixed. A passing Blob chaos report must not be presented as Cosmos chunk-race
or LLM-agent repair coverage.
