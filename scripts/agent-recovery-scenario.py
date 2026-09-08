"""Separate bounded PR183 real-agent resume scenario. Default is a zero-call plan."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time


ROOT = Path(__file__).resolve().parent
LOCK = "pr183-agent-recovery-lock"
CONFIRMATION = "ISOLATED-PR183-AGENT-RESUME-AUTHORIZED"


def load_base():
    spec = importlib.util.spec_from_file_location("pr183_chaos_safety", ROOT / "recovery-chaos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_report(output, prefix):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    reports = []
    for line in (output or "").splitlines():
        if line.startswith("AGENT_RECOVERY_REPORT="):
            report = json.loads(line.split("=", 1)[1])
            if report.get("prefix") == prefix:
                reports.append(report)
    if not reports:
        return {"result": "blocked", "prefix": prefix, "error_code": "pod_report_missing",
                "cleanup": "unverified_inspect_owned_prefix"}
    return reports[-1]


def execute(config, path):
    base = load_base()
    harness = base.Harness(config)
    prefix = "pr183-agent-recovery-" + harness.run_id
    report = {"result": "blocked", "prefix": prefix, "live_scenario": "approved_agent_resume",
              "base_worker_restart_chaos": "not_executed", "lock": LOCK}
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    locked = False
    try:
        # Reuse ONLY existing read-only scope/model/data checks, never its inject/restore methods.
        harness.preflight()
        for name, count in (("omnivec-agent", 1), ("omnivec-cosmos-changefeed", 15)):
            base.require(base.healthy(harness.get("deployment", name), count), "Scenario baseline unavailable: " + name)
        competing = json.loads(harness.kube(
            "get", "configmap", base.LOCK, "--ignore-not-found=true", "-o", "json",
        ) or "{}")
        base.require(not competing, "Base outage harness lock exists; serialize runs")
        harness.kube("create", "-f", "-", document={
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": LOCK, "namespace": config["namespace"]},
            "data": {"run_id": harness.run_id, "prefix": prefix, "scenario": "approved-agent-resume"},
        })
        locked = True
        pods = json.loads(harness.kube("get", "pods", "-l", "app=omnivec-api", "-o", "json"))["items"]
        ready = [p["metadata"]["name"] for p in pods if not p["metadata"].get("deletionTimestamp")
                 and any(c.get("type") == "Ready" and c.get("status") == "True"
                         for c in p.get("status", {}).get("conditions", []))]
        base.require(bool(ready), "No ready API pod")
        command = [
            shutil.which("kubectl") or "kubectl", "--kubeconfig", config["kubeconfig"],
            "--request-timeout=1500s", "-n", config["namespace"], "exec", "-i", ready[0], "--",
            "python3", "-", "--prefix", prefix, "--confirm", CONFIRMATION,
        ]
        try:
            outcome = subprocess.run(
                command, input=(ROOT / "agent_recovery_probe.py").read_text(encoding="utf-8"),
                capture_output=True, text=True, encoding="utf-8", timeout=1500, check=False,
            )
            report.update(decode_report(outcome.stdout, prefix))
            if outcome.returncode and report["result"] == "passed":
                report["result"] = "failed"
                report["error_code"] = "pod_exit_disagrees_with_report"
        except subprocess.TimeoutExpired as error:
            report.update(decode_report(error.stdout, prefix))
            report["result"] = "failed"
            report["error_code"] = "pod_transport_timeout_no_retry"
            report["cleanup"] = "unverified_remote_execution_may_continue"
        # Verify originals and established replica baseline without changing them.
        harness.deadline = time.monotonic() + 180
        after = harness.probe("baseline")
        base.require(after["snapshot"] == harness.baseline["snapshot"]
                     and after["identity"] == harness.baseline["identity"], "Original data/model identity changed")
        base.require(harness.all_healthy(), "Original deployment readiness changed")
        base.require(base.healthy(harness.get("deployment", "omnivec-cosmos-changefeed"), 15),
                     "Established changefeed baseline changed")
        report["originals"] = "unchanged_model_routes_documents_replicas"
    except Exception as error:
        report["result"] = "failed" if report.get("approval_attempts") else "blocked"
        report["host_error"] = type(error).__name__
    finally:
        cleanup = report.get("cleanup")
        safe_cleanup = (isinstance(cleanup, dict) and cleanup.get("status") == "paused"
                        or report.get("fixture_creation_attempted") is False)
        if locked and safe_cleanup and report.get("originals"):
            try:
                harness.deadline = time.monotonic() + 45
                lock = harness.get("configmap", LOCK)
                base.require(lock["data"]["run_id"] == harness.run_id, "Lock ownership changed")
                harness.kube("delete", "configmap", LOCK, "--wait=true", "--timeout=20s", timeout=25)
                report["lock_released"] = True
            except Exception as error:
                report["result"] = "failed"
                report["lock_error"] = type(error).__name__
        elif locked:
            report["lock_retained"] = "inspect_exact_prefix_and_pause_state_before_manual_release"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "recovery-chaos-pr183.json")
    parser.add_argument("--kubeconfig")
    args = parser.parse_args(argv)
    base = load_base()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.kubeconfig:
        config["kubeconfig"] = args.kubeconfig
    base.validate_config(config)
    if not args.execute:
        print(json.dumps({
            "mode": "plan", "external_calls": 0, "live_status": "held_for_parent_clearance",
            "scenario": "one_new_paused_Cosmos_pipeline_one_owned_document_one_agent_resume_approval",
            "endpoint": "https://omnivec-test-v3fapuy5j6s7c.documents.azure.com:443/",
            "database": "testdb", "embedding_model": "mdl-ext-52423101", "chat_model": "mdl-ext-075dd3b9",
            "auth": "existing_API_admin_token_inside_API_pod_via_loopback_authenticated_proxy",
            "approved_action_allowlist": ["resume_pipeline with exactly the newly created pipeline_id"],
            "pass_requires": ["runtime approved-action VERIFIED_PROCESSING",
                              "actual finite1536 distinct chunks matching synthetic source",
                              "increased destination progress", "unchanged originals", "final owned pipeline paused"],
            "manual_resume": "prohibited", "base_worker_restart_chaos": "unchanged_and_not_executed",
            "budget_seconds": {"scenario": 1200, "cleanup": 180, "host_exec": 1500},
        }, indent=2))
        return 0
    base.require(args.confirm == CONFIRMATION, "Separate parent authorization confirmation required")
    base.require(args.report is not None, "New persistent report path required")
    path = args.report.resolve()
    base.require(path.is_relative_to(Path.cwd().resolve()) and path.parent.is_dir() and not path.exists(),
                 "Report must be new and inside current working directory")
    with path.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")
    report = execute(config, path)
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"result": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(2)
