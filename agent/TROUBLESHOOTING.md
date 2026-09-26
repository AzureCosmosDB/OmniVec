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

## Investigation and remediation workflow

1. Identify the exact environment, pipeline, source, destination and responsible
   workload. Establish the time window and collect current read-only evidence.
2. Search the catalog by symptom or retrieve an exact `failure_code`. Check the
   entry's authoritative evidence before selecting a cause. Several failures can
   coexist; resolving one does not prove the others are resolved.
3. Explain the failed operation, identity, affected resource, confidence and
   remaining unknowns. Distinguish data access, resource provisioning, network
   reachability and agent observation failures.
4. Propose only the scoped correction supported by evidence. Mutations require
   operator approval, bounded execution and a rollback/verification plan. Resolve
   actual resource IDs before presenting an executable command; prose runbooks
   are not parameterized scripts and must not be executed verbatim.
5. Verify the failed operation, then the relevant end-to-end data behavior.
   Record skipped stages, workarounds, observation duration and unresolved risks.

The catalog includes these additional investigation paths:

| Area | Failure codes |
|---|---|
| Provisioning capacity | `azure_quota_or_sku_unavailable`, `cosmos_serverless_throughput_rejected` |
| Interrupted installation and images | `helm_pending_release`, `deployment_transport_interruption`, `acr_build_or_import_failure` |
| Identity and permissions | `permission_identity_or_scope_mismatch`, `authorization_denied`, `agent_identity_federation`, `sharepoint_permission_failure` |
| Network and checkpoint bootstrap | `aks_snat_exhaustion`, `cosmos_entra_container_provisioning`, `dns_resolution_failure`, `network_timeout`, `tls_validation_failure` |
| Dependency pressure | `cosmos_request_throttling`, `sharepoint_throttling`, `model_provider_throttling_or_auth`, `servicebus_backlog` |
| Incomplete diagnostics | `agent_diagnostic_coverage_gap`, `stale_health_or_metrics` |
| Data correctness and search | `chunk_update_cleanup_failure`, `duplicate_or_out_of_order_events`, `invalid_embedding`, `search_empty_or_wrong_results` |
| Test prerequisites | `e2e_fixture_or_assertion_mismatch` |

These are investigation guides, not claims that every failure has been reproduced
in live testing or that the agent has tools to apply every fix. Deployment of an
updated agent image is required before local catalog additions become available
to the running agent. Existing diagnostic sampling and Azure metric-collection
limitations are not fixed merely by adding knowledge.

## Verified AKS SNAT recovery

Look up `aks_snat_exhaustion` when Cosmos connections time out from AKS while
other endpoints, or the same Cosmos account from a workstation, remain reachable.
That pattern alone is not proof: compare per-node TCP `UsedSnatPorts`,
`AllocatedSnatPorts`, and failed `SnatConnectionCount` in the same time window.
The deployed diagnostic endpoint does not currently collect these Azure metrics;
an authorized operator must supply that evidence. `UNKNOWN` with Ready pods and
empty queues is not a healthy processing verdict.

In an isolated live test on 2026-09-23, both nodes exhausted their 1,024-port
allocation and accumulated more than 125,000 failed SNAT connections over a
30-minute window. After an approved increase to 8,192 ports per node, Cosmos TCP
connections succeeded from both nodes in 21-27 ms. The first complete observed
post-change minute showed zero failed SNAT connections on both nodes. This is
short-window recovery evidence, not a soak-test result.

The real ingestion probe then passed in **72.51 seconds**: a 5,788-character
document produced 12 distinct, finite, 1,536-dimensional chunk embeddings in
each of two isolated pipelines; semantic search returned the expected topic.
Shrinking the source removed 11 obsolete chunks in the primary pipeline without
changing the other pipeline. Empty-content cleanup and subsequent restoration
passed, and the probe paused both pipelines on exit.

Do not copy 8,192 into every cluster: validate the maximum node count, upgrade
surge, outbound IP capacity and existing configuration first. Changes require
approval and can disrupt outbound connections. Capture the original allocation
for rollback and reconcile the approved setting into infrastructure configuration.
The source of connection churn remains unproven.

This test also required manual checkpoint-container pre-provisioning. The
separate `cosmos_entra_container_provisioning` runbook explains why a Cosmos
403/5300 on container creation requires the management path rather than broader
data-plane roles. Consequently this successful probe does **not** certify
unassisted first-use provisioning.
