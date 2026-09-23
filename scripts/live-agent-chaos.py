"""Bounded scratch916 under-load agent recovery. Default: zero-call plan."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

import agent_recovery_probe as recovery
import recovery_chaos_probe as original


ROOT = Path(__file__).resolve().parent
CONFIRMATION = "ISOLATED-SCRATCH916-LIVE-FLOW-AUTHORIZED"
LOCK = "live-agent-chaos-lock"
SCOPE = {
    "subscription": "074d02eb-4d74-486a-b299-b262264d1536",
    "resource_group": "rg-omnivec-scratch916",
    "cluster": "omnivec-aks-oi2m2sbwv6fhw",
    "namespace": "omnivec",
    "cosmos_account": "omnivec-cosmos-oi2m2sbwv6fhw",
    "cosmos_database": "e2eblob",
    "cosmos_container": "vectors-txt",
    "source_id": "src-db236d4f",
    "destination_id": "dst-742acb58",
    "pipeline_id": "pip-dae87bd8",
    "model_id": "mdl-ext-1320b896",
    "chat_model_id": "mdl-ext-79d53187",
    "route_ids": ["trp-34a1c7fa", "trp-e0728ae8"],
    "blob_account": "omnivecdemo3ce2e45dc0",
    "blob_container": "e2e-fix-916-txt",
    "expected_refs": [
        "azure-blob-storage.txt", "azure-cosmos-db.txt", "azure-kubernetes-service.txt",
    ],
}
WARMUP_DOCUMENTS = 3
STREAM_DOCUMENTS = 24
STREAM_INTERVAL_SECONDS = 3
MARKER = "owned-pending-document"


def validate_prefix(prefix):
    recovery.require(isinstance(prefix, str)
                     and re.fullmatch(r"live-agent-chaos-[0-9a-f]{32}", prefix), "invalid_live_owned_prefix")


def validate_documents(rows, documents, pipeline_id, source_id):
    recovery.require(len(rows) == len(documents) and bool(rows), "live_document_count_mismatch")
    recovery.require(len({row.get("id") for row in rows}) == len(rows), "duplicate_vector_ids")
    refs = [row.get("source_ref") for row in rows]
    recovery.require(len(set(refs)) == len(refs) and set(refs) == set(documents), "live_source_refs_mismatch")
    for row in rows:
        recovery.require(row.get("pipeline_id") == pipeline_id and row.get("source_id") == source_id,
                         "foreign_live_vector")
        recovery.require(row.get("text") == documents[row["source_ref"]]["content"], "live_text_mismatch")
        recovery.require(row.get("chunk_index") == 0 and row.get("chunk_count") == 1,
                         "live_fixture_must_have_one_chunk_per_document")
        recovery.require(original.valid_vector(row.get("embedding")), "invalid_live_embedding")
    recovery.require(len({tuple(row["embedding"]) for row in rows}) == len(rows), "identical_live_embeddings")
    return {"documents": len(rows), "dimensions": 1536, "duplicates": 0, "content_matches": True}


class LiveScenario(recovery.Scenario):
    endpoint = "https://" + SCOPE["cosmos_account"] + ".documents.azure.com:443/"
    database = SCOPE["cosmos_database"]
    embedding_model = SCOPE["model_id"]
    chat_model = SCOPE["chat_model_id"]

    def validate_prefix(self, prefix):
        validate_prefix(prefix)

    def __init__(self, prefix):
        super().__init__(prefix)
        self.started = time.monotonic()
        self.stop_producer = threading.Event()
        self.producer = None
        self.producer_error = None
        self.documents = {}
        self.frozen = None
        self.fault_at = None
        self.event_observer = self.observe_event
        self.original_probe = original.Probe({"config": SCOPE, "run_id": prefix.rsplit("-", 1)[1]})
        self.original_baseline = None
        self.report.update({"scenario": "pause_during_real_processing", "shared_worker_faults": False})

    def stage(self, name):
        self.report["phase"] = name
        self.report.setdefault("timeline", []).append({
            "phase": name, "elapsed_seconds": round(time.monotonic() - self.started, 2),
        })
        self.emit()

    def observe_event(self, event):
        now = time.monotonic()
        if (event.get("type") == "tool_result" and event.get("name") == "diagnose_pipeline"
                and event.get("result", {}).get("scope") == self.pipeline_id):
            self.report["fault_to_diagnosis_seconds"] = round(now - self.fault_at, 2)
        if event.get("type") == "approval_required":
            self.report["fault_to_proposal_seconds"] = round(now - self.fault_at, 2)
        if event.get("type") == "verification" and self.approval_at is not None:
            self.report["approval_to_verification_seconds"] = round(now - self.approval_at, 2)

    def preflight(self):
        self.stage("read_only_preflight")
        super().preflight()
        self.original_baseline = self.original_probe.baseline()
        self.report["original_documents_before"] = self.original_baseline["documents"]

    def owned_pipeline(self):
        pipe = super().owned_pipeline()
        for kind, item_id, suffix in (
            ("sources", self.source_id, "-source"),
            ("destinations", self.destination_id, "-vectors"),
        ):
            item = self.call("GET", kind + "/" + item_id)
            config = item.get("config", {})
            recovery.require(item.get("name") == self.prefix + suffix and item.get("enabled", True),
                             "live_registration_ownership_changed")
            recovery.require(config.get("endpoint", "").rstrip("/") == self.endpoint.rstrip("/")
                             and config.get("database") == self.database
                             and config.get("container") == self.prefix + suffix
                             and config.get("auth_type") == "managed-identity",
                             "live_registration_binding_changed")
        return pipe

    def write_document(self, ident, *, marker=False):
        if marker:
            content = ("A silver kestrel rescue station maps coral reef storms with underwater sonar beacons. "
                       "Its marine biologists coordinate reef rescue vessels.")
        else:
            content = (f"Observatory reading {ident}. A cobalt otter station studies polar aurora "
                       "using violet spectrometers and magnetometers.")
        content += " Isolated reliability fixture " + self.prefix + "."
        recovery.require(len(content) < recovery.CHUNK["chunk_size"], "live_document_size_bound")
        document = {"id": ident, "content": content, "owner": self.prefix}
        self.source.create_item(document)
        self.documents[ident] = document

    def produce(self):
        try:
            for index in range(STREAM_DOCUMENTS):
                if self.stop_producer.is_set():
                    break
                self.remaining()
                self.write_document(f"stream-{index:03d}")
                if self.stop_producer.wait(STREAM_INTERVAL_SECONDS):
                    break
        except Exception as error:
            self.producer_error = type(error).__name__

    def require_producer_ok(self):
        recovery.require(self.producer_error is None, "synthetic_producer_failed")

    def create_fixture(self):
        self.stage("creating_new_empty_fixture")
        self.create_resources()
        for index in range(WARMUP_DOCUMENTS):
            self.write_document(f"warmup-{index:03d}")
        self.stage("waiting_for_real_warmup_vectors")
        self.wait(lambda: len(self.rows()) >= WARMUP_DOCUMENTS, 300, "initial_processing_not_observed")
        validate_documents(self.rows(), dict(self.documents), self.pipeline_id, self.source_id)
        self.wait_for_cached_health()
        self.stage("feeding_documents_while_processing")
        self.producer = threading.Thread(target=self.produce, name="owned-chaos-producer", daemon=True)
        self.producer.start()
        self.wait(lambda: len(self.rows()) > WARMUP_DOCUMENTS, 60, "processing_under_load_not_observed")
        self.require_producer_ok()
        recovery.require(self.producer.is_alive()
                         and len(self.documents) < WARMUP_DOCUMENTS + STREAM_DOCUMENTS,
                         "producer_finished_before_fault")
        before = self.rows()
        recovery.require(self.owned_pipeline().get("status") == "active", "fixture_not_active_before_fault")
        self.report["under_load_evidence"] = {
            "warmup_vectors": WARMUP_DOCUMENTS, "vectors_before_pause": len(before),
            "source_documents_before_pause": len(self.documents), "producer_running_at_pause": True,
        }
        self.stage("injecting_pause_into_only_owned_pipeline")
        self.fault_at = time.monotonic()
        self.call("POST", "pipelines/" + self.pipeline_id + "/pause")
        recovery.require(self.owned_pipeline().get("status") == "paused", "live_pause_failed")
        self.report["fault_injected"] = ["owned_pipeline_paused_during_processing"]
        time.sleep(35)
        self.require_producer_ok()
        self.write_document(MARKER, marker=True)
        rows = self.rows()
        self.frozen = original.snapshot(rows)
        self.initial_embedded_count = len(rows)
        recovery.require(self.initial_embedded_count > WARMUP_DOCUMENTS
                         and not any(row.get("source_ref") == MARKER for row in rows),
                         "pending_fault_not_established")
        time.sleep(5)
        self.validate_paused_fault()
        self.report["observed_failure"] = {
            "pipeline": "paused", "persisted_vectors": len(rows),
            "pending_source_documents": len(self.documents) - len(rows),
            "post_fault_marker_not_indexed": True,
        }
        self.stage("fault_observed_waiting_for_agent")

    def validate_paused_fault(self):
        self.require_producer_ok()
        recovery.require(self.owned_pipeline().get("status") == "paused"
                         and original.snapshot(self.rows()) == self.frozen,
                         "paused_destination_changed_before_approval")

    def request_and_approve(self):
        self.stage("requesting_scoped_diagnosis_and_one_resume")
        super().request_and_approve()
        self.stage("approved_agent_action_returned")

    def verify_data(self):
        self.producer.join(timeout=min(120, self.remaining()))
        self.require_producer_ok()
        recovery.require(not self.producer.is_alive(), "producer_completion_deadline")
        expected_count = WARMUP_DOCUMENTS + STREAM_DOCUMENTS + 1
        recovery.require(len(self.documents) == expected_count, "bounded_load_not_fully_submitted")

        def check():
            rows = self.rows()
            if len(rows) < expected_count:
                return None
            return validate_documents(rows, self.documents, self.pipeline_id, self.source_id)

        proof = self.wait(check, 180, "live_recovery_document_deadline")
        time.sleep(5)
        recovery.require(check() == proof, "live_recovery_not_stable")
        source_rows = list(self.source.query_items("SELECT * FROM c", enable_cross_partition_query=True))
        recovery.require(len(source_rows) == expected_count
                         and all(row.get("id") in self.documents
                                 and all(row.get(key) == value
                                         for key, value in self.documents[row["id"]].items())
                                 for row in source_rows), "source_data_changed")
        result = self.original_probe.request("/api/playground/search", {
            "query": "How do silver kestrel marine biologists use sonar to rescue coral reefs?",
            "destination_ids": [self.destination_id], "top_k": 3,
        })
        recovery.require(bool(result.get("results"))
                         and result["results"][0].get("source_ref") == MARKER, "recovered_search_top_result_wrong")
        recovery.require(self.owned_pipeline().get("status") == "active", "repaired_pipeline_not_active")
        self.report.update({
            "processing_recovery": proof,
            "recovered_marker_search": "correct_top_result",
            "repair_actor": "approved_real_agent_resume_pipeline",
            "agent_repair": "VERIFIED_PROCESSING_AND_ACTUAL_DOCUMENTS",
        })
        self.stage("processing_and_search_verified")

    def cleanup(self):
        self.stage("stopping_owned_producer_and_pausing_owned_pipeline")
        self.stop_producer.set()
        if self.producer is not None:
            self.producer.join(timeout=30)
        try:
            super().cleanup()
        finally:
            if self.original_baseline is not None:
                after = self.original_probe.baseline()
                recovery.require(after == self.original_baseline, "original_demo_changed")
                self.report["originals"] = "unchanged_vectors_bindings_and_search"
        recovery.require(self.producer is None or not self.producer.is_alive(), "producer_not_stopped")
        if self.producer_error:
            self.report["producer_error_type"] = self.producer_error


def load_commands():
    spec = importlib.util.spec_from_file_location("live_chaos_commands", ROOT / "recovery-chaos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pod_payload():
    modules = {name: (ROOT / (name + ".py")).read_text(encoding="utf-8")
               for name in ("agent_recovery_probe", "recovery_chaos_probe")}
    payload = base64.b64encode(json.dumps({
        "modules": modules, "main": Path(__file__).read_text(encoding="utf-8"),
    }).encode()).decode()
    return (
        "import base64,json,sys,types\n"
        f"payload=json.loads(base64.b64decode({payload!r}))\n"
        "for name,source in payload['modules'].items():\n"
        " module=types.ModuleType(name);module.__file__='/tmp/'+name+'.py';sys.modules[name]=module\n"
        " exec(compile(source,module.__file__,'exec'),module.__dict__)\n"
        "exec(compile(payload['main'],'/tmp/live-agent-chaos.py','exec'),"
        "{'__name__':'__main__','__file__':'/tmp/live-agent-chaos.py'})\n"
    )


def execute_host(kubeconfig, report_path):
    common = load_commands()
    commands = common.Commands()
    prefix = "live-agent-chaos-" + uuid.uuid4().hex
    report = {"result": "blocked", "prefix": prefix, "fault_injected": [], "approval_attempts": 0}
    lock_created = False
    baseline = None
    kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", SCOPE["cluster"],
            "--request-timeout=30s", "-n", SCOPE["namespace"]]

    def query(*args):
        return json.loads(commands.run(kube + list(args), label="read-only Kubernetes query"))

    def deployments():
        items = query("get", "deployments", "-o", "json")["items"]
        recovery.require(bool(items), "deployment_baseline_missing")
        result = {}
        for item in items:
            count = item["spec"].get("replicas", 1)
            recovery.require(common.healthy(item, count), "deployment_not_ready")
            result[item["metadata"]["name"]] = {
                "replicas": count,
                "template": hashlib.sha256(json.dumps(item["spec"]["template"], sort_keys=True).encode()).hexdigest(),
            }
        recovery.require({"omnivec-api", "omnivec-agent", "omnivec-cosmos-changefeed",
                          "omnivec-dotnet-worker"}.issubset(result), "required_workload_missing")
        return result

    try:
        recovery.require(kubeconfig.is_file(), "explicit_kubeconfig_missing")
        cluster = json.loads(commands.run([
            "az", "aks", "show", "--subscription", SCOPE["subscription"],
            "--resource-group", SCOPE["resource_group"], "--name", SCOPE["cluster"],
            "--query", "{id:id,fqdn:fqdn,privateFqdn:privateFqdn}", "-o", "json", "--only-show-errors",
        ], timeout=45, label="read-only Azure cluster identity"))
        expected_id = (f"/subscriptions/{SCOPE['subscription']}/resourceGroups/{SCOPE['resource_group']}"
                       f"/providers/Microsoft.ContainerService/managedClusters/{SCOPE['cluster']}")
        recovery.require(cluster.get("id", "").lower() == expected_id.lower(), "wrong_azure_cluster")
        config = query("config", "view", "--minify", "-o", "json")["clusters"][0]["cluster"]
        allowed = {"https://" + name.lower() + suffix
                   for name in (cluster.get("fqdn"), cluster.get("privateFqdn")) if name
                   for suffix in ("", ":443")}
        recovery.require(config["server"].rstrip("/").lower() in allowed
                         and not config.get("insecure-skip-tls-verify"), "wrong_or_insecure_kubeconfig")
        for name in ("pr183-recovery-chaos-lock", "pr183-agent-recovery-lock", LOCK):
            existing = commands.run(kube + ["get", "configmap", name, "--ignore-not-found=true", "-o", "name"],
                                    label="read-only chaos lock check")
            recovery.require(not existing.strip(), "another_chaos_run_requires_reconciliation")
        baseline = deployments()
        commands.run(kube + ["create", "-f", "-"], stdin=json.dumps({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": LOCK, "namespace": SCOPE["namespace"]},
            "data": {"prefix": prefix, "scenario": "owned-pipeline-pause-under-load"},
        }), label="create exclusive owned chaos lock")
        lock_created = True
        pods = query("get", "pods", "-l", "app=omnivec-api", "-o", "json")["items"]
        ready = [pod["metadata"]["name"] for pod in pods if not pod["metadata"].get("deletionTimestamp")
                 and any(condition.get("type") == "Ready" and condition.get("status") == "True"
                         for condition in pod.get("status", {}).get("conditions", []))]
        recovery.require(bool(ready), "ready_api_pod_missing")
        report.update({"phase": "executing_bounded_pod_scenario", "original_workloads": baseline})
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        output = commands.run(
            kube[:6] + ["--request-timeout=1500s", "-n", SCOPE["namespace"],
                        "exec", "-i", ready[0], "--", "python3", "-", "--pod", "--execute",
                        "--confirm", CONFIRMATION, "--prefix", prefix],
            stdin=pod_payload(), timeout=1500, label="bounded live scenario; never retry",
        )
        reports = [json.loads(line.split("=", 1)[1]) for line in output.splitlines()
                   if line.startswith("AGENT_RECOVERY_REPORT=")]
        matching = [item for item in reports if item.get("prefix") == prefix]
        recovery.require(bool(matching), "owned_pod_report_missing")
        report.update(matching[-1])
        recovery.require(deployments() == baseline, "shared_workload_baseline_changed")
        report["shared_workloads"] = "unchanged_and_ready"
    except Exception as error:
        report["result"] = "failed" if lock_created else "blocked"
        report["host_error"] = str(error) if isinstance(error, recovery.Blocked) else type(error).__name__
        if isinstance(error, common.CommandFailure):
            report["command_failure"] = error.evidence
    finally:
        cleanup = report.get("cleanup")
        safe = isinstance(cleanup, dict) and cleanup.get("status") == "paused"
        if (lock_created and safe and report.get("originals")
                and report.get("shared_workloads") == "unchanged_and_ready"):
            try:
                lock = query("get", "configmap", LOCK, "-o", "json")
                recovery.require(lock.get("data", {}).get("prefix") == prefix, "chaos_lock_ownership_changed")
                commands.run(kube + ["delete", "configmap", LOCK, "--wait=true", "--timeout=20s"],
                             label="release verified owned chaos lock")
                report["lock_released"] = True
            except Exception as error:
                report["result"] = "failed"
                report["lock_error_type"] = type(error).__name__
        elif lock_created:
            report["lock_retained"] = "reconcile_exact_prefix_before_any_other_run"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--pod", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prefix", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({
            "mode": "plan", "external_calls": 0, "environment": "scratch916",
            "fault": "pause only a newly created pipeline after real under-load writes",
            "maximum_source_documents": WARMUP_DOCUMENTS + STREAM_DOCUMENTS + 1,
            "maximum_approved_actions": 1, "allowed_repair": "resume_pipeline for exact new pipeline",
            "shared_workload_mutations": False, "manual_resume": False,
            "pass_requires": ["processing before fault", "producer active at fault",
                              "actual scoped diagnosis", "single validated approval",
                              "runtime VERIFIED_PROCESSING", "all real vectors and correct marker search",
                              "original demo and workloads unchanged", "final owned pipeline paused"],
        }, indent=2))
        return 0
    recovery.require(args.confirm == CONFIRMATION, "explicit_live_flow_authorization_required")
    if args.pod:
        validate_prefix(args.prefix)
        report = LiveScenario(args.prefix).run()
        # Completed scenario reports must survive a failing test's exec transport.
        return 0 if report.get("prefix") == args.prefix else 1
    recovery.require(args.kubeconfig is not None and args.report is not None,
                     "explicit_kubeconfig_and_new_report_required")
    path = args.report.resolve()
    recovery.require(path.parent.is_dir(), "report_parent_missing")
    with path.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")
    report = execute_host(args.kubeconfig.resolve(), path)
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"result": "blocked", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(2)
