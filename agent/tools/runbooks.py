"""Searchable, read-only troubleshooting knowledge for known OmniVec failures."""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import tool


class RunbookQuery(BaseModel):
    query: str = Field(default="", max_length=200)
    component: str = Field(default="", max_length=80)
    failure_code: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_.-]*$")
    limit: int = Field(default=6, ge=1, le=12)


def _case(
    component: str,
    symptoms: list[str],
    checks: list[str],
    causes: list[str],
    actions: list[str],
    avoid: list[str],
    verification: list[str],
) -> dict:
    return {
        "component": component,
        "symptoms": symptoms,
        "authoritative_checks": checks,
        "likely_causes": causes,
        "safe_actions": actions,
        "avoid": avoid,
        "recovery_verification": verification,
    }


RUNBOOKS: dict[str, dict] = {
    "pod_crash_loop": _case(
        "kubernetes",
        ["CrashLoopBackOff", "repeated container restarts", "non-zero last exit code"],
        ["pod status and last termination reason", "bounded current and previous logs", "deployment generation and events"],
        ["invalid configuration", "dependency unavailable", "startup exception", "liveness probe killing a slow startup"],
        ["fix the observed configuration or dependency", "restart one controller-owned pod only after the cause is corrected"],
        ["repeated blind restarts", "deleting every pod", "claiming recovery from Running state alone"],
        ["ready replicas converge", "restart count stabilizes", "pipeline destination progress increases"],
    ),
    "pod_oomkilled": _case(
        "kubernetes",
        ["OOMKilled", "exit code 137", "memory working set at limit"],
        ["last container state", "memory request/limit and peak metrics", "document or batch size near failure"],
        ["oversized document", "unbounded batch", "memory leak", "limit below normal working set"],
        ["quarantine the offending input", "bound batch/document size", "raise limits only with measured headroom"],
        ["unbounded limit increases", "retrying the same poison input indefinitely"],
        ["no new OOM events during a representative load window", "owned work completes exactly once"],
    ),
    "image_pull_failure": _case(
        "deployment",
        ["ImagePullBackOff", "ErrImagePull", "manifest unknown", "unauthorized registry pull"],
        ["pod events", "exact deployment image and digest", "ACR manifest existence", "kubelet identity AcrPull assignment"],
        ["missing tag", "wrong registry", "ACR authorization", "architecture mismatch"],
        ["restore the intended immutable image digest", "repair AcrPull on the kubelet identity", "roll out and observe convergence"],
        ["using an unrelated latest image", "embedding registry credentials in manifests"],
        ["all replicas use the intended digest", "readiness converges", "application smoke test succeeds"],
    ),
    "pod_pending_unschedulable": _case(
        "kubernetes",
        ["Pending", "FailedScheduling", "insufficient CPU or memory", "untolerated taint"],
        ["pod scheduling events", "node readiness and allocatable resources", "affinity, selectors, taints, quotas and PDBs"],
        ["capacity exhausted", "incorrect node selector", "unavailable node pool", "quota or autoscaler backoff"],
        ["correct scheduling constraints", "restore or scale the intended node pool within budget"],
        ["removing safety taints globally", "scheduling system workloads onto incompatible nodes"],
        ["pod scheduled on the intended pool", "node remains Ready", "workload readiness and processing progress converge"],
    ),
    "node_unreachable": _case(
        "kubernetes",
        ["NodeStatusUnknown", "node.kubernetes.io/unreachable", "kubelet stopped posting status"],
        ["confirm kubeconfig targets the intended AKS cluster", "AKS node-pool and VMSS power state", "node conditions and leases"],
        ["stale kubeconfig", "VM or network failure", "kubelet failure", "control-plane connectivity loss"],
        ["switch to the correct cluster before repair", "repair or replace only the affected pool using Azure-supported operations"],
        ["acting on a similarly named cluster", "removing unreachable taints before node recovery"],
        ["all intended nodes Ready", "system pods healthy", "application replicas and external endpoint healthy"],
    ),
    "rollout_stuck": _case(
        "deployment",
        ["rollout timeout", "old replicas never terminate", "observed generation behind"],
        ["deployment conditions", "new pod events and readiness", "PDB and resource capacity", "image digest"],
        ["bad image", "failed readiness", "insufficient surge capacity", "PDB conflict"],
        ["fix the new replica cause or roll back to the last verified revision"],
        ["forcing deletion without checking PDB/data ownership", "declaring success from image update alone"],
        ["desired, updated, ready and available replicas converge on one generation", "smoke and processing checks pass"],
    ),
    "dns_resolution_failure": _case(
        "network",
        ["no such host", "temporary failure in name resolution", "NXDOMAIN"],
        ["resolve from the affected pod", "CoreDNS readiness and logs", "service name, namespace and private DNS links"],
        ["wrong hostname", "CoreDNS outage", "private DNS misconfiguration", "network path to DNS blocked"],
        ["correct the endpoint", "restore DNS components or private-zone linkage"],
        ["hard-coding transient IP addresses", "restarting unrelated application pods repeatedly"],
        ["repeated DNS resolution succeeds from the workload", "dependency request and pipeline progress succeed"],
    ),
    "network_timeout": _case(
        "network",
        ["connect timeout", "read timeout", "connection reset", "intermittent dependency failures"],
        ["bounded connection probe from the workload", "network policy and firewall", "dependency latency and throttling"],
        ["firewall or policy denial", "dependency saturation", "SNAT exhaustion", "regional incident"],
        ["restore the network path", "use bounded exponential retry only for idempotent operations"],
        ["unbounded retries", "retrying ambiguous writes", "disabling TLS validation"],
        ["latency/error thresholds hold through a soak window", "no duplicate writes", "backlog drains"],
    ),
    "tls_validation_failure": _case(
        "network",
        ["certificate signed by unknown authority", "certificate expired", "hostname mismatch"],
        ["certificate chain and expiry", "requested hostname/SNI", "trusted CA configuration"],
        ["expired certificate", "private CA not mounted", "endpoint hostname changed"],
        ["rotate the certificate or trust the approved CA", "keep hostname validation enabled"],
        ["turning off certificate verification", "accepting arbitrary certificates"],
        ["TLS handshake succeeds with hostname validation", "normal authenticated request succeeds"],
    ),
    "agent_identity_federation": _case(
        "identity",
        ["AADSTS700213", "no matching federated identity record", "managed identity token unavailable"],
        ["service account annotations", "AKS OIDC issuer", "federated credential subject and audience", "AZURE_CLIENT_ID"],
        ["wrong subject", "wrong issuer or audience", "identity bound to another service account"],
        ["repair the dedicated identity federation with api://AzureADTokenExchange"],
        ["reusing a more privileged identity", "falling back to embedded secrets"],
        ["token acquisition succeeds in the intended pod", "required read-only observations become known"],
    ),
    "authorization_denied": _case(
        "identity",
        ["401", "403", "Forbidden", "AuthorizationPermissionMismatch", "AccessDenied"],
        ["identify the exact caller identity", "resource data-plane role and scope", "credential expiry and audience"],
        ["missing RBAC or Graph grant", "wrong identity", "expired credential", "firewall masking as authorization"],
        ["restore least-privilege access at the narrowest resource scope", "wait for propagation and retest"],
        ["requesting credentials in chat", "granting owner/contributor broadly", "bypassing authorization"],
        ["authenticated operation succeeds", "health check is fresh", "real read/write progress is observed"],
    ),
    "sharepoint_permission_failure": _case(
        "source-sharepoint",
        ["Graph 403", "site or drive inaccessible", "SharePoint source health unhealthy"],
        ["site_id and drive_id", "Sites.Selected application permission", "site-specific read grant", "broad Graph grants"],
        ["missing admin consent", "missing site grant", "wrong site/drive ID", "permission propagation delay"],
        ["grant Sites.Selected and read on only the configured site", "remove Files.Read.All or Sites.Read.All after validation"],
        ["creating a second app registration unnecessarily", "granting tenant-wide file access"],
        ["Graph drive lookup succeeds", "folder enumeration succeeds", "new document reaches the destination exactly once"],
    ),
    "sharepoint_throttling": _case(
        "source-sharepoint",
        ["Graph 429", "Retry-After", "slow or stalled polling"],
        ["response status and Retry-After", "poll interval", "request concurrency", "delta/page continuation"],
        ["polling too frequently", "parallel enumeration", "tenant throttling"],
        ["honor Retry-After", "reduce concurrency", "resume from the saved continuation or delta token"],
        ["tight retry loops", "discarding checkpoints", "full rescans as routine recovery"],
        ["requests remain below throttle threshold", "checkpoint advances", "no duplicate documents"],
    ),
    "blob_event_delivery_stalled": _case(
        "source-blob",
        ["new blobs not ingested", "Event Grid delivery failures", "blob queue remains empty"],
        ["Event Grid subscription and webhook status", "BlobEventConsumerEnabled", "queue depth", "blob ingestor readiness"],
        ["disabled consumer", "invalid webhook", "Event Grid authorization", "network failure"],
        ["restore the existing subscription/consumer", "use bounded polling fallback only when configured"],
        ["recreating subscriptions without ownership checks", "resetting checkpoints", "deleting queued events"],
        ["owned test blob event is observed", "vector appears once", "deletion propagates normally"],
    ),
    "cosmos_changefeed_stalled": _case(
        "source-cosmos",
        ["change feed not advancing", "leases unchanged", "source documents exceed embedded count"],
        ["changefeed deployment readiness", "lease ownership and continuation", "source and destination health", "throttling"],
        ["worker outage", "lease conflict", "invalid continuation", "429 throttling", "partition hot spot"],
        ["restore workers", "resolve lease ownership", "honor retry-after while preserving continuation"],
        ["deleting lease containers", "resetting offsets without explicit reprocessing approval"],
        ["lease continuation advances", "destination count increases", "no duplicate or stale vectors"],
    ),
    "postgres_polling_stalled": _case(
        "source-postgresql",
        ["rows not ingested", "timestamp cursor unchanged", "database connection failures"],
        ["host/database/table", "ID and timestamp columns", "read permission", "poll interval and cursor"],
        ["missing index", "incorrect timestamp semantics", "clock skew", "firewall or credential failure"],
        ["correct schema/cursor configuration", "add an appropriate source index", "resume from the preserved cursor"],
        ["rewinding the cursor blindly", "storing plaintext credentials", "full-table polling at high frequency"],
        ["new row is processed once", "cursor advances monotonically", "source load remains bounded"],
    ),
    "onelake_fabric_failure": _case(
        "onelake",
        ["Fabric job rejected", "job remains pending", "OneLake 403 or throttling", "staging file missing"],
        ["workspace/lakehouse/job IDs", "identity workspace role", "staging path", "job state and timestamps"],
        ["missing Fabric permission", "invalid item ID", "throttling", "stale job race", "staging cleanup race"],
        ["restore scoped Fabric access", "honor throttling", "fence job attempts and retain staging evidence"],
        ["submitting duplicate ambiguous jobs", "deleting staging data before completion is proven"],
        ["one authoritative job completes", "target table advances", "checkpoint and staging cleanup are consistent"],
    ),
    "servicebus_backlog": _case(
        "messaging",
        ["active message count grows", "workers stopped or saturated", "pipeline lag increases"],
        ["queue/subscription depth", "worker desired/ready replicas", "throughput and oldest-message age", "DLQ count"],
        ["worker outage", "downstream throttling", "insufficient capacity", "poison-message retry loop"],
        ["restore or scale only the responsible worker", "fix the downstream bottleneck", "drain under observation"],
        ["purging backlog", "scaling without a ceiling", "assuming an empty queue proves destination writes"],
        ["backlog decreases", "destination progress increases", "error rate and duplicates stay within threshold"],
    ),
    "dead_letter_messages": _case(
        "messaging",
        ["dead-letter count above zero", "repeated processing failures"],
        ["peek bounded DLQ samples", "error reason and source/pipeline identity", "current code/config compatibility"],
        ["poison input", "schema mismatch", "transient dependency exhausted retries"],
        ["fix the cause", "approve a bounded replay with duplicate handling and ownership checks"],
        ["purging to make health green", "bulk replay without limits", "logging sensitive payloads"],
        ["replayed owned messages succeed exactly once", "DLQ decreases by the expected count", "no foreign messages touched"],
    ),
    "duplicate_or_out_of_order_events": _case(
        "pipeline",
        ["duplicate vectors", "older update overwrites newer content", "delete followed by stale recreation"],
        ["source version/etag", "event ordering metadata", "destination IDs and chunk indices", "checkpoint generation"],
        ["at-least-once delivery", "concurrent updates", "missing revision fencing", "non-idempotent writes"],
        ["use deterministic IDs and version fencing", "ignore stale revisions", "make replay idempotent"],
        ["last-write-wins without version evidence", "deleting all vectors before a replacement is durable"],
        ["latest source version is represented exactly once", "obsolete chunks are absent", "replay produces no changes"],
    ),
    "checkpoint_missing_or_corrupt": _case(
        "pipeline",
        ["checkpoint absent", "continuation rejected", "unexpected full replay"],
        ["checkpoint ownership, generation and timestamp", "source continuation validity", "destination duplicate state"],
        ["storage loss", "schema/version change", "partial write", "manual deletion"],
        ["fail closed", "restore a verified checkpoint or perform an explicitly approved bounded backfill"],
        ["silently starting from zero", "resetting offsets as routine repair"],
        ["checkpoint advances atomically", "backfill bounds are recorded", "no duplicates or lost documents"],
    ),
    "pipeline_no_progress": _case(
        "pipeline",
        ["active pipeline with unchanged embedded count", "pending jobs", "zero throughput"],
        ["two observations over a source-appropriate window", "source count, jobs, queues, workers and dependencies"],
        ["quiet source", "worker outage", "dependency failure", "stale health", "poll interval longer than sample"],
        ["diagnose the named pipeline", "fix the observed dependency", "allow one targeted restart only with evidence"],
        ["calling active status healthy", "repeated restarts", "short-window conclusions for polling sources"],
        ["destination progress increases between ordered observations", "dependencies are fresh and healthy"],
    ),
    "model_missing_or_wrong_category": _case(
        "model",
        ["model not found", "route missing", "chat model used for embeddings"],
        ["pipeline model/route binding", "registered model category and enabled state", "provider deployment"],
        ["deleted registration", "route drift", "wrong model selected"],
        ["restore the existing model/route binding through configuration workflow"],
        ["silently substituting a model", "changing model IDs during recovery"],
        ["same intended model ID resolves", "real finite embedding is produced", "pipeline writes resume"],
    ),
    "vector_dimension_mismatch": _case(
        "destination",
        ["dimension mismatch", "vector policy rejects writes", "search errors"],
        ["embedding dimension", "destination vector field/index dimensions", "pipeline binding"],
        ["model changed", "wrong index selected", "destination created with incompatible policy"],
        ["align model and destination through an explicit migration plan while preserving IDs/data"],
        ["truncating or padding embeddings", "silently switching models", "dropping the destination"],
        ["real embedding dimension matches policy", "write and semantic query both succeed"],
    ),
    "cosmos_vector_write_failure": _case(
        "destination-cosmos",
        ["Cosmos write 403/429", "vector index missing", "partition-key error"],
        ["data contributor role and container scope", "Retry-After", "partition key", "vector embedding policy"],
        ["missing data-plane role", "throttling", "wrong partition key", "incompatible vector policy"],
        ["restore scoped contributor access", "honor throttling", "correct configuration through migration workflow"],
        ["account-key fallback", "unbounded retries", "recreating a populated container automatically"],
        ["write succeeds once", "vector index is reported", "query returns the owned document"],
    ),
    "pgvector_schema_failure": _case(
        "destination-pgvector",
        ["vector extension missing", "column not found", "dimension or type error"],
        ["CREATE EXTENSION vector availability", "table and column schema", "write permission", "SSL mode"],
        ["extension not installed", "schema drift", "wrong vector column", "credential/firewall issue"],
        ["install/enable pgvector through the database owner", "correct the configured columns", "use bounded retries"],
        ["auto-altering production tables", "disabling TLS", "logging database passwords"],
        ["schema discovery succeeds", "write and nearest-neighbor query succeed", "dimensions match"],
    ),
    "garnet_unavailable_or_data_loss": _case(
        "destination-garnet",
        ["Garnet endpoint unavailable", "VSIM fails", "vector set empty after restart"],
        ["endpoint/TLS/auth", "Garnet process and persistence configuration", "OneLake authoritative table"],
        ["pod restart without persistence", "memory pressure", "wrong vector set", "preview feature disabled"],
        ["restore Garnet", "rebuild the mirror from the authoritative OneLake table when explicitly approved"],
        ["treating the cache as authoritative", "claiming durability without restart testing"],
        ["mirror count and sampled vectors match OneLake", "search succeeds before and after restart"],
    ),
    "invalid_embedding": _case(
        "data-integrity",
        ["empty, zero, non-finite, Boolean, wrong-length or identical embeddings"],
        ["actual persisted vector values", "model response shape", "dimension and per-document distinctness"],
        ["provider error masked as success", "serialization bug", "stub response", "wrong model route"],
        ["reject invalid vectors", "fix the provider/serialization path", "reprocess only owned affected documents"],
        ["accepting HTTP 200 as embedding proof", "persisting placeholder vectors"],
        ["finite nonzero vectors of exact dimension", "distinct inputs produce appropriate distinct vectors", "semantic query works"],
    ),
    "stale_health_or_metrics": _case(
        "observability",
        ["health says healthy while processing is stalled", "missing or old checked_at", "metrics gap"],
        ["health timestamp freshness", "authoritative resource state", "two destination-progress observations"],
        ["cached health", "telemetry outage", "wrong time range", "silent adapter fallback"],
        ["mark state UNKNOWN", "restore observations", "use direct bounded checks"],
        ["converting missing data to zero", "declaring healthy from stale checks"],
        ["fresh checks agree with resource state", "metrics resume", "real processing evidence is present"],
    ),
    "configuration_drift": _case(
        "deployment",
        ["portal, CLI, Helm or API accept different connector fields", "live image differs from configured tag"],
        ["shared connector contract", "Helm values and live deployment", "image digest", "API validation"],
        ["manual kubectl change", "mutable tag", "partial rollout", "duplicated schemas"],
        ["reconcile through the maintained deployment path", "use shared contracts and immutable build evidence"],
        ["editing live resources without persisting configuration", "assuming latest refers to the intended digest"],
        ["contract tests pass", "live digest matches release", "rollout and connector smoke tests pass"],
    ),
}


def _terms(code: str, entry: dict) -> str:
    values = [code, entry["component"]]
    for key in ("symptoms", "likely_causes"):
        values.extend(entry[key])
    return " ".join(values).lower()


@tool(
    "get_troubleshooting_runbook",
    "Search known OmniVec failure scenarios. Use after collecting diagnostic evidence to get safe checks, remediation boundaries, anti-patterns, and recovery proof requirements.",
    RunbookQuery,
)
async def get_troubleshooting_runbook(params: RunbookQuery) -> dict:
    failure_code = params.failure_code.strip().lower()
    component = params.component.strip().lower()
    words = [word for word in params.query.lower().split() if len(word) > 1]

    if failure_code:
        entry = RUNBOOKS.get(failure_code)
        return {
            "matches": [{"failure_code": failure_code, **entry}] if entry else [],
            "unknown": None if entry else f"No exact runbook for {failure_code}; diagnose from evidence and escalate unknowns.",
        }

    ranked = []
    for code, entry in RUNBOOKS.items():
        if component and component not in entry["component"].lower():
            continue
        haystack = _terms(code, entry)
        score = sum(1 for word in words if word in haystack)
        if words and score == 0:
            continue
        ranked.append((score, code, entry))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return {
        "matches": [{"failure_code": code, **entry} for _, code, entry in ranked[:params.limit]],
        "catalog_size": len(RUNBOOKS),
        "guidance": "Runbooks guide investigation; only current tool evidence can establish the active failure or prove recovery.",
    }
