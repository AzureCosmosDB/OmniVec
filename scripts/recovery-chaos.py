"""Bounded PR183 integration chaos. Default plan mode performs no external calls."""

import argparse
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


ROOT = Path(__file__).resolve().parent
CONFIRMATION = "ISOLATED-PR183-CHAOS-AUTHORIZED"
LOCK = "pr183-recovery-chaos-lock"
WORKER = "omnivec-dotnet-worker"
SIGNAL_RUN = "omnivec.io/pr183-chaos-run"
SIGNAL_PHASE = "omnivec.io/pr183-chaos-phase"
DEPLOYMENTS = {
    WORKER: 2, "docgrok": 1, "docgrok-controller": 1,
    "omnivec-api": 2, "omnivec-blob-ingestor": 1,
}
RESTARTS = ["docgrok", "docgrok-controller", "omnivec-api", "omnivec-blob-ingestor"]
# Deliberately not a generic/customer-cluster allowlist.
ALLOWLIST = {
    "subscription": "074d02eb-4d74-486a-b299-b262264d1536",
    "resource_group": "rg-omnivec-omnivec-pr183-test",
    "cluster": "omnivec-aks-v3fapuy5j6s7c",
    "namespace": "omnivec",
    "source_id": "src-1b2aac6d",
    "pipeline_id": "pip-11f0ffed",
    "destination_id": "dst-ef1231b2",
    "model_id": "mdl-ext-52423101",
    "route_ids": ["trp-34a1c7fa", "trp-e0728ae8"],
    "blob_account": "omnivecdemo9918e79d45",
    "blob_container": "e2e-blob-txt",
    "cosmos_account": "omnivec-cosmos-v3fapuy5j6s7c",
    "cosmos_database": "e2eblob",
    "cosmos_container": "vectors-txt",
    "servicebus_namespace": "omnivec-sb-v3fapuy5j6s7c",
    "topic": "embeddings",
    "subscription_name": "worker",
    "expected_refs": [
        "azure-blob-storage.txt", "azure-cosmos-db.txt",
        "azure-kubernetes-service.txt", "chaos-backlog.txt",
    ],
}


class Blocked(RuntimeError):
    pass


_STDERR_SIGNALS = [
    (r"\bforbidden\b", "forbidden", "authorization"),
    (r"\bunauthorized\b|you must be logged in", "unauthorized", "authentication"),
    (r"x509:|certificate signed by unknown authority|certificate has expired", "TLS certificate validation failed", "tls_certificate"),
    (r"\bnotfound\b|\bnot found\b", "resource not found", "not_found"),
    (r"\balreadyexists\b|\balready exists\b", "resource already exists", "already_exists"),
    (r"\btoo many requests\b|\b429\b", "server throttling", "throttled"),
    (r"\bi/o timeout\b", "i/o timeout", "timeout"),
    (r"tls handshake timeout", "TLS handshake timeout", "timeout"),
    (r"context deadline exceeded|client\.timeout exceeded|request timed out|timed out|timeout expired",
     "request deadline exceeded", "timeout"),
    (r"unable to connect to the server", "unable to connect to the server", "transport"),
    (r"connection refused|connection reset|connection aborted", "connection refused/reset/aborted", "transport"),
    (r"\bno such host\b|temporary failure in name resolution", "DNS resolution failure", "transport"),
    (r"unexpected eof|connection closed|http2.*goaway", "transport closed unexpectedly", "transport"),
    (r"error dialing backend|error sending request|unable to upgrade connection", "Kubernetes backend transport failure", "transport"),
    (r"\bserviceunavailable\b|\bservice unavailable\b|\b503\b", "service unavailable", "service_unavailable"),
    (r"executable .* not found|executable file not found", "credential executable unavailable", "executable_unavailable"),
]
_PROBE_ERROR_TYPES = {
    "AssertionError", "RuntimeError", "TimeoutError", "ConnectionError", "HTTPError",
    "HttpResponseError", "ClientAuthenticationError", "CredentialUnavailableError",
    "ServiceRequestError", "ServiceResponseError", "ResourceNotFoundError",
    "PermissionError", "ValueError", "KeyError", "ModuleNotFoundError", "ImportError",
}
_READ_RESOURCES = {"deployment", "deployments", "pod", "pods", "job", "jobs",
                   "configmap", "configmaps", "hpa"}


def diagnostic_excerpt(stderr):
    """Project stderr onto fixed diagnostic phrases; never copy arbitrary text."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    text = (stderr or "")[-32768:]
    matches = [(phrase, category) for pattern, phrase, category in _STDERR_SIGNALS
               if re.search(pattern, text, re.I)]
    return {
        "classification": matches[0][1] if matches else "nonzero_exit",
        "stderr_excerpt": ("; ".join(phrase for phrase, _ in matches[:4])[:384]
                           if matches else "[unrecognized stderr omitted]" if text else ""),
        "stderr_redaction": "fixed_diagnostic_phrases_only",
    }


class CommandFailure(RuntimeError):
    def __init__(self, operation, *, returncode=None, stderr=None, stdout=None, classification=None):
        evidence = diagnostic_excerpt(stderr)
        if classification:
            if classification == "timeout":
                evidence["process_timed_out"] = True
            if classification != "timeout" or evidence["classification"] not in {
                "authentication", "authorization", "tls_certificate", "not_found", "already_exists", "throttled",
            }:
                evidence["classification"] = classification
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        for line in (stdout or "")[-8192:].splitlines():
            if line.startswith("CHAOS_ERROR=") and line[12:] in _PROBE_ERROR_TYPES:
                evidence["probe_error_type"] = line[12:]
                if classification is None and evidence["classification"] == "nonzero_exit":
                    evidence["classification"] = "pod_probe_error"
                break
        self.evidence = {"operation": operation, "returncode": returncode, **evidence}
        super().__init__(operation + ": " + evidence["classification"] + " (raw output suppressed)")


def retryable_get(args, document):
    # kubectl lists through its built-in get verb; never retry arbitrary plugins,
    # raw URLs, streams, execs or writes even if their stderr looks transient.
    return (
        document is None and len(args) >= 2 and args[0] == "get"
        and args[1].split("/", 1)[0] in _READ_RESOURCES and args[1].count("/") <= 1
        and not any(arg == "-w" or arg.startswith(("-w=", "--watch", "--raw")) for arg in args[2:])
    )


def require(condition, message):
    if not condition:
        raise Blocked(message)


def validate_config(config):
    require(set(config) == set(ALLOWLIST) | {"kubeconfig"}, "Unknown or missing configuration fields")
    for key, value in ALLOWLIST.items():
        require(config[key] == value, "PR183 allowlist mismatch: " + key)
    require(bool(config["kubeconfig"]), "Explicit kubeconfig required")


def healthy(deployment, replicas):
    status = deployment.get("status", {})
    return (
        deployment["spec"]["replicas"] == replicas
        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
        and all(status.get(field, 0) == replicas for field in (
            "replicas", "updatedReplicas", "readyReplicas", "availableReplicas",
        ))
        and not status.get("unavailableReplicas", 0)
    )


class Commands:
    def run(self, args, *, timeout=30, stdin=None, label="command"):
        try:
            result = subprocess.run(
                [shutil.which(args[0]) or args[0], *args[1:]],
                input=stdin, capture_output=True, text=True, timeout=timeout, check=False,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as error:
            raise CommandFailure(label, stderr=error.stderr, stdout=error.stdout,
                                 classification="timeout") from None
        except OSError:
            raise CommandFailure(label, classification="executable_unavailable") from None
        if result.returncode:
            raise CommandFailure(label, returncode=result.returncode, stderr=result.stderr, stdout=result.stdout)
        return result.stdout


class Harness:
    def __init__(self, config, *, commands=None, clock=time.monotonic, sleep=time.sleep):
        self.c = config
        self.commands = commands or Commands()
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + 1500
        self.run_id = uuid.uuid4().hex
        self.job = "pr183-chaos-watchdog-" + self.run_id[:12]
        self.locked = False
        self.watchdog_possible = False
        self.outage_possible = False
        self.fixture_possible = False
        self.signal_possible = False
        self.originals = {}
        self.baseline = None
        self.stage = "initialization"
        self.report = {
            "run_id": self.run_id, "result": "blocked", "fault_injected": [],
            "observed_failure": [], "repair_actor": [], "readiness": "not_checked",
            "processing_recovery": "not_checked", "data_model_identity_and_duplicates": "not_checked",
            "cleanup": "not_needed", "watchdog": self.job, "lock": LOCK,
            "autonomous_conversational_repair": "not_exercised_by_base_harness",
            "diagnostic_recovery_tools": "not_integrated_by_base_harness",
            "not_covered": ["production message replay", "out-of-order Cosmos chunk race",
                            "network denial", "model deletion", "multi-chunk update/deletion"],
        }

    def command(self, args, timeout=30, stdin=None, label="command"):
        self.report["last_operation"] = label
        remaining = self.deadline - self.clock()
        require(remaining > 0, "Harness deadline exceeded")
        return self.commands.run(args, timeout=min(timeout, remaining), stdin=stdin, label=label)

    def kube(self, *args, timeout=30, document=None):
        read_only = retryable_get(args, document)
        operation = "kubectl " + args[0] + (" " + args[1].split("/", 1)[0] if read_only else "")

        def invoke(budget):
            return self.command(
                ["kubectl", "--kubeconfig", self.c["kubeconfig"],
                 "--request-timeout=" + str(max(1, int(budget))) + "s",
                 "-n", self.c["namespace"], *args],
                timeout=budget, stdin=None if document is None else json.dumps(document),
                label=operation,
            )
        if not read_only:
            return invoke(timeout)
        deadline = min(self.deadline, self.clock() + timeout)
        for attempt in range(1, 4):
            remaining = deadline - self.clock()
            require(remaining > 0, "Read-only command deadline exceeded")
            try:
                return invoke(min(10, remaining))
            except CommandFailure as error:
                retry = (error.evidence["classification"] in ("timeout", "transport")
                         and attempt < 3 and self.clock() + attempt < deadline)
                self.report.setdefault("read_attempt_failures", []).append({
                    "stage": self.stage, "attempt": attempt, "will_retry": retry, **error.evidence,
                })
                if not retry:
                    raise
                self.sleep(attempt)

    def record_failure(self, field, error):
        details = {
            "stage": self.stage, "operation": self.report.get("last_operation", "not_started"),
            "error_type": type(error).__name__, "returncode": None,
            "classification": ("precondition_failed" if isinstance(error, Blocked) else
                               "timeout" if isinstance(error, TimeoutError) else "non_command_error"),
        }
        if isinstance(error, CommandFailure):
            details.update(error.evidence)
        # Keep the original stage even when cleanup subsequently changes last_operation.
        self.report.setdefault(field, details)

    def get(self, kind, name):
        return json.loads(self.kube("get", kind, name, "-o", "json"))

    def pause_until(self, check, seconds, label):
        deadline = min(self.deadline, self.clock() + seconds)
        while self.clock() < deadline:
            result = check()
            if result:
                return result
            self.sleep(2)
        raise RuntimeError(label + ": deadline exceeded")

    def all_healthy(self):
        return all(healthy(self.get("deployment", name), count) for name, count in self.originals.items())

    def preflight(self):
        validate_config(self.c)
        require(Path(self.c["kubeconfig"]).is_file(), "Explicit kubeconfig does not exist")
        cluster = json.loads(self.command([
            "az", "aks", "show", "--subscription", self.c["subscription"],
            "--resource-group", self.c["resource_group"], "--name", self.c["cluster"],
            "--query", "{id:id,fqdn:fqdn,privateFqdn:privateFqdn}", "-o", "json",
        ], label="Azure AKS identity", timeout=45))
        expected_id = (
            "/subscriptions/" + self.c["subscription"] + "/resourceGroups/" +
            self.c["resource_group"] + "/providers/Microsoft.ContainerService/managedClusters/" +
            self.c["cluster"]
        )
        require(cluster["id"].lower() == expected_id.lower(), "Unexpected Azure cluster identity")
        context = json.loads(self.kube("config", "view", "--minify", "-o", "json"))
        server = context["clusters"][0]["cluster"]["server"].rstrip("/").lower()
        allowed = {"https://" + fqdn.lower() + suffix for fqdn in
                   (cluster.get("fqdn"), cluster.get("privateFqdn")) if fqdn for suffix in ("", ":443")}
        require(server in allowed, "Kubeconfig server is not the explicitly allowlisted AKS")
        require(not context["clusters"][0]["cluster"].get("insecure-skip-tls-verify"), "Insecure kubeconfig denied")
        hpas = json.loads(self.kube("get", "hpa", "-o", "json"))["items"]
        require(not any(hpa["spec"]["scaleTargetRef"]["name"] in DEPLOYMENTS for hpa in hpas),
                "HPA may race restoration; serialize and explicitly remove test HPA separately")
        for name, expected in DEPLOYMENTS.items():
            deployment = self.get("deployment", name)
            require(healthy(deployment, expected), "Unhealthy or rolling baseline: " + name)
            if name == WORKER:
                annotations = deployment["metadata"].get("annotations", {})
                require(SIGNAL_RUN not in annotations and SIGNAL_PHASE not in annotations,
                        "Prior watchdog signal remains; inspect previous run before retry")
            self.originals[name] = deployment["spec"]["replicas"]
        self.report["original_replicas"] = dict(self.originals)
        self.baseline = self.probe("baseline")
        self.report["readiness"] = "healthy_baseline"
        self.report["baseline"] = self.baseline

    def acquire(self):
        # create, never apply: Kubernetes atomically refuses concurrent runs.
        self.kube("create", "-f", "-", document={
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": LOCK, "namespace": self.c["namespace"],
                         "labels": {"app.kubernetes.io/part-of": "pr183-recovery-chaos"}},
            "data": {"run_id": self.run_id, "phase": "preflight", "watchdog": self.job},
        })
        self.locked = True

    def phase(self, value):
        lock = self.get("configmap", LOCK)
        require(lock["data"]["run_id"] == self.run_id, "Run lock ownership changed")
        lock["data"]["phase"] = value
        lock["data"]["original_replicas"] = json.dumps(self.originals)
        self.kube("replace", "-f", "-", document=lock)
        # Deployment metadata (not pod-template metadata) does not cause a rollout.
        # The existing API service account can read/patch deployments but cannot
        # read ConfigMaps, so the independent watchdog reads this narrow signal.
        worker = self.get("deployment", WORKER)
        annotations = worker["metadata"].get("annotations", {})
        require(annotations.get(SIGNAL_RUN) in (None, self.run_id), "Worker signal belongs to another run")
        self.signal_possible = True
        self.kube("patch", "deployment", WORKER, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": worker["metadata"]["resourceVersion"],
                         "annotations": {SIGNAL_RUN: self.run_id, SIGNAL_PHASE: value}},
        }))

    def probe(self, action):
        payload = base64.b64encode(json.dumps({
            "config": self.c, "run_id": self.run_id, "baseline": self.baseline,
        }).encode()).decode()
        pods = json.loads(self.kube("get", "pods", "-l", "app=omnivec-api", "-o", "json"))["items"]
        ready = [p["metadata"]["name"] for p in pods
                 if not p["metadata"].get("deletionTimestamp") and
                 any(c["type"] == "Ready" and c["status"] == "True"
                     for c in p.get("status", {}).get("conditions", []))]
        require(bool(ready), "No ready API pod for pod-local probe")
        # Do not retry mutating probes: an exec transport timeout is ambiguous.
        code = (ROOT / "recovery_chaos_probe.py").read_text(encoding="utf-8")
        output = self.command([
            "kubectl", "--kubeconfig", self.c["kubeconfig"], "--request-timeout=150s",
            "-n", self.c["namespace"], "exec", "-i", ready[0], "--",
            "python3", "-c", code, action, payload,
        ], timeout=155, label="pod probe " + action)
        results = [line[len("CHAOS_PROBE="):] for line in output.splitlines() if line.startswith("CHAOS_PROBE=")]
        require(len(results) == 1, "Missing structured probe result")
        return json.loads(results[0])

    def establish_watchdog(self):
        api = self.get("deployment", "omnivec-api")
        settings = {
            "namespace": self.c["namespace"], "worker": WORKER,
            "replicas": self.originals[WORKER], "run_id": self.run_id, "lock": LOCK,
            "arm_timeout": 120, "outage_seconds": 180, "restore_seconds": 600,
        }
        template = api["spec"]["template"]["spec"]
        job = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": self.job, "namespace": self.c["namespace"],
                         "labels": {"pr183-chaos-run": self.run_id}},
            "spec": {
                "backoffLimit": 0, "activeDeadlineSeconds": 1200,
                # No TTL: retain failed watchdog evidence and protection until verified.
                "template": {"metadata": {"labels": {"pr183-chaos-run": self.run_id}}, "spec": {
                    "serviceAccountName": template.get("serviceAccountName", "default"),
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "watchdog", "image": template["containers"][0]["image"],
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "-u", "-c",
                                    (ROOT / "recovery_chaos_watchdog.py").read_text(encoding="utf-8"),
                                    json.dumps(settings)],
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "250m", "memory": "256Mi"}},
                    }],
                }},
            },
        }
        self.watchdog_possible = True
        self.kube("create", "-f", "-", document=job)
        self.kube("wait", "--for=condition=Ready", "pod", "-l", "job-name=" + self.job,
                  "--timeout=60s", timeout=65)
        self.pause_until(
            lambda: "WATCHDOG_READY" in self.kube("logs", "job/" + self.job),
            30, "Watchdog readiness and RBAC dry-run",
        )

    def inject(self):
        self.phase("armed")
        self.outage_possible = True
        self.report["fault_injected"].append({"fault": "worker_outage", "status": "requested", "max_seconds": 180})
        self.kube("scale", "deployment/" + WORKER, "--replicas=0")
        self.pause_until(
            lambda: healthy(self.get("deployment", WORKER), 0), 45, "Worker outage",
        )
        self.report["fault_injected"][-1]["status"] = "confirmed"
        self.fixture_possible = True
        self.probe("upload")
        observation = self.probe("queued")
        require(healthy(self.get("deployment", WORKER), 0), "Watchdog restored before queue observation")
        self.report["observed_failure"].append({
            "failure": "owned_work_queued_without_processing", "evidence": observation,
        })
        for name in RESTARTS:
            self.report["fault_injected"].append({"fault": "rollout_restart", "deployment": name, "status": "requested"})
            self.kube("rollout", "restart", "deployment/" + name)
            self.report["fault_injected"][-1]["status"] = "submitted"

    def restore(self):
        self.deadline = self.clock() + 420
        if self.watchdog_possible:
            self.report["repair_actor"].append("harness_finally_replica_restore")
            # Mark the attempt before the command: the API response can be lost.
            self.kube("scale", "deployment/" + WORKER, "--replicas=" + str(self.originals[WORKER]))
            self.pause_until(self.all_healthy, 330, "Actual deployment restoration")
            self.report["readiness"] = "restored_all_original_replicas_and_rollouts"
            self.phase("restored")
            self.kube("wait", "--for=condition=complete", "job/" + self.job, "--timeout=40s", timeout=45)
            logs = self.kube("logs", "job/" + self.job)
            if "WATCHDOG_RESTORE_ATTEMPT" in logs:
                self.report["repair_actor"].append("independent_kubernetes_watchdog")
            require("WATCHDOG_RESTORED" in logs or "WATCHDOG_DISARMED" in logs,
                    "Watchdog has not acknowledged verified restoration")
            require(self.all_healthy(), "Restoration regressed; retain watchdog")
            self.kube("delete", "job", self.job, "--wait=true", "--timeout=20s", timeout=25)
            self.watchdog_possible = False

    def execute(self):
        error = None
        restored = False
        try:
            # Identity and baseline reads precede any writes.
            self.stage = "preflight"
            self.preflight()
            self.stage = "lock_acquisition"
            self.acquire()
            # Recheck after acquiring the lock; never rely on an unlocked stale baseline.
            self.stage = "baseline_recheck"
            require(self.all_healthy(), "Baseline changed while acquiring lock")
            self.baseline = self.probe("baseline")
            self.phase("preflight")
            self.stage = "watchdog_setup"
            self.establish_watchdog()
            self.stage = "fault_injection"
            self.inject()
        except Exception as exc:
            error = exc
            self.record_failure("primary_failure", exc)
        finally:
            if self.watchdog_possible:
                try:
                    self.stage = "restoration"
                    self.restore()
                    restored = True
                except Exception as exc:
                    self.record_failure("restoration_failure", exc)
                    self.report["restoration_error"] = type(exc).__name__
                    self.report["readiness"] = "restoration_unverified_watchdog_and_lock_retained"
                    error = error or exc
            else:
                restored = not self.outage_possible
        if restored and self.fixture_possible:
            self.deadline = self.clock() + 480
            try:
                if error is None:
                    self.stage = "processing_verification"
                    data = self.probe("verify")
                    self.stage = "model_identity_verification"
                    identity = self.probe("registry")
                    require(identity == self.baseline["identity"], "Model or route identities changed")
                    self.stage = "search_verification"
                    self.probe("search")
                    self.report["processing_recovery"] = "passed_real_persisted_embedding_and_search"
                    self.report["data_model_identity_and_duplicates"] = data
            except Exception as exc:
                self.record_failure("primary_failure", exc)
                error = error or exc
            finally:
                try:
                    self.stage = "fixture_cleanup"
                    self.report["cleanup"] = self.probe("cleanup")
                except Exception as exc:
                    self.record_failure("cleanup_failure", exc)
                    self.report["cleanup"] = "failed_owned_fixture_only; lock_retained"
                    error = error or exc
        if self.locked and restored and (not isinstance(self.report["cleanup"], str) or not self.fixture_possible):
            try:
                self.stage = "lock_cleanup"
                self.release_lock()
            except Exception as exc:
                self.record_failure("lock_cleanup_failure", exc)
                error = error or exc
        if error:
            self.report["error_type"] = type(error).__name__
            # Never stringify dependency exceptions: they can contain credentials.
            self.report["result"] = "failed" if self.outage_possible else "blocked"
        else:
            self.report["result"] = "passed"
        return self.report

    def release_lock(self):
        lock = self.get("configmap", LOCK)
        require(lock["data"]["run_id"] == self.run_id, "Refusing to delete another run's lock")
        if self.signal_possible:
            worker = self.get("deployment", WORKER)
            require(worker["metadata"].get("annotations", {}).get(SIGNAL_RUN) == self.run_id,
                    "Refusing to remove another run's worker signal")
            self.kube("patch", "deployment", WORKER, "--type=merge", "-p", json.dumps({
                "metadata": {"resourceVersion": worker["metadata"]["resourceVersion"],
                             "annotations": {SIGNAL_RUN: None, SIGNAL_PHASE: None}},
            }))
        self.kube("delete", "configmap", LOCK, "--wait=true", "--timeout=15s", timeout=20)
        self.locked = False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "recovery-chaos-pr183.json")
    parser.add_argument("--kubeconfig", help="Explicit kubeconfig override; AKS server is still allowlisted")
    parser.add_argument("--namespace", help="Explicit namespace; only the isolated allowlist is accepted")
    parser.add_argument("--source-id")
    parser.add_argument("--destination-id")
    parser.add_argument("--execute", action="store_true", help="LIVE DISRUPTION: requires serialized parent authorization")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report", type=Path, help="New report file under the current working directory")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for field in ("kubeconfig", "namespace", "source_id", "destination_id"):
        if getattr(args, field) is not None:
            config[field] = getattr(args, field)
    validate_config(config)
    if not args.execute:
        print(json.dumps({
            "mode": "plan", "external_calls": 0, "namespace": config["namespace"],
            "cluster": config["cluster"], "source": config["source_id"],
            "destination": config["destination_id"],
            "steps": ["allowlist_and_healthy_baseline", "atomic_cluster_lock",
                      "independent_watchdog_ready_and_RBAC_dry_run",
                      "worker_outage_and_owned_blob_upload", "peek_owned_queued_message",
                      "restart_router_controller_API_ingestor", "finally_restore_and_verify",
                      "verify_originals_model_routes_embeddings_search_no_duplicates",
                      "delete_exact_run_owned_blob_and_wait_for_EventGrid_cleanup"],
            "watchdog_outage_seconds": 180, "normal_deadline_seconds": 1500,
            "restoration_budget_seconds": 420, "post_recovery_budget_seconds": 480,
            "live_status": "blocked_until_parent_serializes_rollouts_and_authorizes",
        }, indent=2))
        return 0
    require(args.confirm == CONFIRMATION, "Explicit isolated PR183 confirmation required")
    require(args.report is not None, "--report required for persistent live evidence")
    report_path = args.report.resolve()
    require(report_path.is_relative_to(Path.cwd().resolve()), "Report must be inside current working directory")
    require(report_path.parent.is_dir() and not report_path.exists(), "Report must be new with an existing parent")
    # Reserve evidence before mutation. No customer config, credential or raw logs are saved.
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump({"result": "blocked", "state": "starting"}, stream)
    harness = Harness(config)
    try:
        report = harness.execute()
    except BaseException as error:
        harness.record_failure("primary_failure", error)
        report = harness.report
        report["result"] = "failed" if harness.outage_possible else "blocked"
        report["error_type"] = type(error).__name__
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Blocked as error:
        print(json.dumps({"result": "blocked", "reason": str(error)}))
        raise SystemExit(2)
