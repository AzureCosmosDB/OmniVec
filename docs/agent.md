# OmniVec agent: system and pipeline troubleshooting

The existing **in-cluster OmniVec agent** diagnoses operational faults, proposes
concrete scoped fixes, executes approved tools and verifies recovery. It is not
a separate agent product. The web Agent tab and `omnivec agent chat` use the
existing `/api/agent/*` proxy, internal token, caller roles and user sessions.
The service has no public ingress.

## Operational contract

1. **Diagnose:** `diagnose_system` or `diagnose_pipeline(pipeline_id)` correlates
   configuration, source/destination health, embedding model/route bindings,
   deployment and pod readiness/restarts, sampled events/log signals, actual
   Service Bus queue **and embedding topic subscription** backlog/DLQ counters,
   jobs and destination progress. Evidence includes timestamp, likely cause,
   confidence, unknowns and an actionable runbook.
2. **Choose a bounded fix:** use the returned allowlisted action candidate when
   appropriate. A stopped worker can propose `scale_deployment`; a paused
   pipeline can propose `resume_pipeline`. A controller-owned pod may be
   restarted after triage. Infrastructure permissions, source credentials,
   configuration errors and unknown faults require the appropriate owner.
3. **Approve:** admin-only mutations show the existing Approve/Deny card.
   Approval is caller-scoped and consumed once; reusing a tool-call ID does not
   authorize another action. Readers cannot mutate or approve.
4. **Execute and verify:** the runtime, not just the prompt, takes a baseline,
   executes once and observes up to two post-action snapshots. It records a
   structured `recovery` result in tool results, audit summaries and the stored
   final session message. Model-written success prose is replaced by the
   runtime's authoritative recovery conclusion.
5. **Stop or escalate:** at most two approved actions per recovery continuation.
   No automatic restart loop, model replacement, purge or offset reset.
   A new explicit operator turn can begin another investigation.

Supply `pipeline_id` on pod/scale tools when repairing a particular pipeline.
Without it, verification is system-scoped and may be incomplete in larger
deployments. Each snapshot has a 35-second total deadline, 12-second observation
deadlines, at most six pipelines/eight sources per pipeline, eight models per
pipeline, eight queues/eight subscriptions, 100 pods/deployments and three
80-line/16KB log samples. Verification waits five seconds between samples.
Slow ingestion may need a later `verify_recovery` observation; the short window
must not be misrepresented as proof of a dead poller.

### Health states

| State | Meaning |
|---|---|
| `HEALTHY` | Destination embedded count increased between observations with unchanged generation/model/destination bindings, current dependency health, ready workloads and no observed blocking failures. |
| `READY_IDLE` | Dependencies ready and no observed outstanding work; **processing/repair is not verified**. |
| `BLOCKED` | Concrete blocker such as disabled/stopped workload, missing model or failed dependency. |
| `UNHEALTHY` | Observed failed jobs or dead letters requiring triage. |
| `UNKNOWN` | Missing, stale, warning, failed or incomplete evidence, or no progress in the bounded window. |

`active`, HTTP 200, empty queues, successful tool execution and rollout success
are **not** processing proof. Health checks older than ten minutes are unknown.
Queue counters are shared infrastructure evidence, not proof that one pipeline
owns the messages. Log pattern matches may be historical or unrelated; only
signal codes are included in structured diagnostics, not raw secret-bearing
logs/configuration. Credential-shaped fields are scrubbed from tool results.
Counts cannot prove semantic embedding quality or every document's correctness.

## Practical runbooks

| Observed problem | Concrete response and verification |
|---|---|
| Missing embedding model/routing binding | Restore the **referenced** model/route through the existing model configuration workflow. Preserve model IDs/dimensions. Check fresh model health and increasing destination count. Do not replace it with an unrelated model or chat deployment. |
| Stopped worker with backlog | Check intentional maintenance/HPA first; approve scaling the named worker deployment to one replica. Observe pods, subscription backlog/DLQ, jobs and actual destination writes. No queue purge. |
| Unready/crashing worker | Inspect readiness/restarts and events/log signals. Fix the demonstrated dependency/configuration cause; at most one targeted controller-owned pod restart per proposal. Escalate if still blocked. |
| Poison message / DLQ | Inspect failure metadata without exposing payload secrets. Correct the cause before bounded operator replay; account for duplicate delivery. The built-in replay facade currently **fails closed** rather than pretending to move messages. Purging never counts as repair. |
| Disabled Blob event consumer | Enable the existing `ChangeFeed__BlobEventConsumerEnabled` setting through Helm. Verify Event Grid delivery, `blob-events` consumption, embedding subscription processing and destination writes. Do not recreate triggers blindly. |
| Source permissions/credentials | Source owner restores required access for the existing identity. Do not bypass auth or request secrets in chat. Wait for fresh source health and verify ingestion. |
| Dead poller / no progress | Compare source/destination/job counters over a source-appropriate window and inspect the responsible controller, change-feed processor, blob ingestor or SharePoint watcher. Quiet/idle alone is not a fault. Preserve checkpoints; fix the proven cause before restarting. |
| Bad chunk config | Correct size > 0, 0 ≤ overlap < size and unit `chars`/`tokens` in the existing edit flow. Preserve IDs/checkpoints; verify newly processed chunks. The agent does not silently rewrite pipeline configuration. |

Destructive reset/purge and cancellation are excluded from routine repair.
They require an explicit destructive user request and a scoped approval card
describing data loss/cancellation or full reprocessing impact. The purge facade
is not configured and fails closed. Existing pipeline pause/resume and job
retry/cancel use the real control-plane endpoints.

## Runtime configuration

The agent reads model metadata from OmniVec's `/api/models` registry and invokes
the registered provider's HTTPS chat-completion endpoint. **DocGrok handles
embeddings; it does not expose the old assumed registry chat route.** Configure a
**registered chat-capable model** using `agent.defaultModelId` /
`AGENT_DEFAULT_MODEL_ID`, or select a chat model override in the UI/request.
Embedding models, including `text-embedding-3-small`, are rejected for chat.
Azure OpenAI defaults to the agent's workload identity, which needs Cognitive
Services OpenAI User access on the **approved** account. The registry's stored
endpoint must be an Azure public-cloud `*.openai.azure.com` or
`*.cognitiveservices.azure.com` account endpoint for workload-identity mode;
the agent will not send that bearer to an arbitrary compatible host. The stored
API key is not exposed to the agent through model-list responses. For key-based
providers, explicitly select `api-key` mode and provide a Kubernetes secret
bound to the selected registered model ID; a different model override cannot
receive that key. Supported providers are `azure-openai`, `openai` and
`openai-compatible`, with an HTTPS base endpoint and deployment/model name.
No chat model is provisioned automatically. Provider errors and missing model
configuration are reported explicitly, not presented as successful LLM tests.
Liveness/readiness only describe agent service availability.

| Setting | Purpose |
|---|---|
| `INTERNAL_API_TOKEN` | Existing API-to-agent bearer secret; never expose it to browsers or logs. |
| `OMNIVEC_API_URL`, `DOCGROK_URL` | Existing in-cluster service URLs. No forged Host header. |
| `agent.chatAuthMode` | `managed-identity` (default, Azure OpenAI) or explicit `api-key`; sets `AGENT_CHAT_AUTH_MODE`. |
| `agent.chatApiKeySecret: {name, key, modelId}` | Optional secret reference for `AGENT_CHAT_API_KEY`; required `modelId` binds it to `AGENT_CHAT_API_KEY_MODEL_ID`. Do not put plaintext keys in Helm. |
| `agent.apiTokenSecret: {name, key}` | Optional Kubernetes secret reference for outbound `OMNIVEC_API_TOKEN`; use when control-plane service auth requires a bearer. No plaintext token values in Helm. |
| `OMNIVEC_NAMESPACE` | Hard namespace boundary for Kubernetes operations. |
| `agent.servicebusFqns` | Service Bus FQNS; defaults to `azure.serviceBus.namespace`. |
| `agent.queueNames` | Comma-separated queue allowlist (`AGENT_QUEUE_NAMES`); default `blob-events`. |
| `agent.servicebusSubscriptions` | Comma-separated `topic/subscription` allowlist (`AGENT_SB_SUBSCRIPTIONS`); default configured embeddings topic + `/worker`. |
| `agent.allowKubernetesRemediation` | Default absent/false. Explicit opt-in grants pod delete and named deployment-scale patch permissions; approval/namespace/workload guards still apply. |

The Service Bus read adapter uses workload identity and the management API.
Set an entity allowlist explicitly to `""` when that entity type is not deployed.
Missing identity/management permissions or a missing entity produces UNKNOWN,
never a fabricated zero. Default RBAC is read-only for pods/logs/events and
deployments. Kubernetes repair remains blocked until the operator enables its
RBAC. No arbitrary shell, code execution, secret reads or cross-namespace
actions are exposed as tools.

### Deterministic diagnostics without an LLM

Authenticated internal endpoints (same `INTERNAL_API_TOKEN` plus validated
`X-Caller-Id`/`X-Caller-Role` contract as the API proxy):

* `GET /v1/diagnostics/system`
* `POST /v1/diagnostics/pipeline` with `{"pipeline_id":"pip-..."}`

These read-only endpoints do not require a chat deployment and write a concise
diagnostic audit record. They are internal service endpoints; do not expose
the internal token or create a public ingress for them. The existing API proxy
is unchanged; conversational use discovers the same functions as agent tools.
`verify_recovery` also works as a read-only tool with optional pipeline ID.

### Supplying a permitted chat deployment

First obtain an approved **existing chat-capable deployment** and its HTTPS
account endpoint, deployment name and supported API version. Register its
metadata in the existing Models UI or authenticated `POST /api/models`:

```json
{
  "name": "operations-chat",
  "model_category": "chat",
  "type": "azure-openai",
  "endpoint": "https://<approved-account>.openai.azure.com",
  "deployment": "<existing-chat-deployment>",
  "api_version": "2024-06-01",
  "auth_type": "managed-identity"
}
```

Set `agent.defaultModelId` to the returned ID (not the deployment name). Keep
all embedding model IDs/routes unchanged. Registration only stores metadata;
it does **not** provision a model, validate quota, grant identity permissions or
prove tool-calling capability. After configuration, test an ordinary read-only
chat question through the existing authenticated agent proxy, then verify a
diagnostic tool call before considering conversational runtime ready.

If no permitted deployment exists, report the missing account/region,
chat model/version, deployment SKU/capacity, quota and identity configuration.
Do not create a billable Azure deployment or substitute an embedding model to
make the configuration appear complete.

### Current limitations

* Sessions, approval records and audit storage currently use the existing
  in-memory stores, not a configured durable Cosmos backend. Pod replacement
  loses them; an operator must reissue an expired/lost approval.
* Legacy Cosmos diagnostic facade is not wired; it now returns explicit
  unavailable errors instead of fake zero/empty observations. Structured
  pipeline diagnostics use real control-plane stats and health checks.
* DLQ replay/purge are not wired to a real adapter and cannot claim success.
* Cached health checks may lag repair; system coverage over six pipelines and
  short observation windows deliberately result in UNKNOWN.
* Read-only deterministic validation is distinct from live conversational
  testing and from executing approved repairs. Do not claim one proves another.

## Validation

Run the existing pytest suite with mocks (no network by default):

```powershell
python -m pytest tests\unit\test_agent_diagnostics.py tests\unit\test_agent_llm.py tests\unit\test_agent_phase2.py tests\unit\test_agent_session_audit.py tests\unit\test_agent_tools.py tests\unit\test_agent_loop.py tests\unit\test_agent_auth.py -q
```

Regressions cover role/approval isolation, single-use approvals, destructive
repair exclusion, bounded action/verification loops, failed/stub observations,
worker backlog, dependencies/model routing, Blob consumers, invalid chunking,
progress vs idle/unknown, binding/checkpoint changes and authoritative final
repair results.
