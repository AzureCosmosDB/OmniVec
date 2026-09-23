# OmniVec troubleshooting knowledge

The agent exposes the read-only `get_troubleshooting_runbook` tool. Its catalog
is maintained in `agent/tools/runbooks.py` and covers known failures across:

- Kubernetes scheduling, node health, pod lifecycle, image pulls and rollouts
- DNS, TLS, timeouts, firewall and network-policy failures
- Workload identity federation, RBAC, Graph and credential failures
- Azure Blob, SharePoint, Cosmos DB, PostgreSQL and OneLake ingestion
- Service Bus backlog, dead letters, replay and poison-message handling
- Pipeline progress, checkpoints, ordering, idempotency and chunk configuration
- Model registration, routing and vector-dimension compatibility
- Cosmos vector, pgvector, OneLake and Garnet destination failures
- Invalid embeddings, duplicate/lost/stale data and deletion propagation
- Stale health, telemetry gaps and deployment/configuration drift

Every runbook contains symptoms, authoritative checks, likely causes, safe
actions, actions to avoid, and recovery-verification requirements.

Runbooks do not establish root cause. The agent must first call
`diagnose_pipeline` or `diagnose_system`, then select only runbooks supported by
current evidence. Missing or stale observations remain `UNKNOWN`. Recovery
requires real destination progress or scenario-specific data-integrity evidence;
HTTP success, pod readiness, an empty queue, or an active pipeline is not enough.

Mutating operations remain approval-gated and bounded by the recovery runtime.
The catalog must never recommend exposing credentials, bypassing authorization,
purging shared queues, resetting checkpoints, silently changing model IDs, or
repeating ambiguous writes.
