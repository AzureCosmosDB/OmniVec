"""Offline fault injection only: no subprocess, Azure or Kubernetes access."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chaos = load("recovery_chaos", "recovery-chaos.py")
probe = load("recovery_chaos_probe", "recovery_chaos_probe.py")
watchdog = load("recovery_chaos_watchdog", "recovery_chaos_watchdog.py")


@pytest.fixture
def config():
    return json.loads((SCRIPTS / "recovery-chaos-pr183.json").read_text())


def test_default_plan_has_no_external_calls(monkeypatch, capsys):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(side_effect=AssertionError("external call")))
    assert chaos.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "plan" and plan["external_calls"] == 0


@pytest.mark.parametrize("field,value", [
    ("namespace", "customer"), ("subscription", "other"), ("cluster", "production"),
    ("source_id", "src-other"), ("destination_id", "dst-other"),
    ("blob_container", "customer-docs"), ("topic", "production-dlq"),
])
def test_scope_allowlist_fails_closed(config, field, value):
    config[field] = value
    with pytest.raises(chaos.Blocked, match="allowlist mismatch"):
        chaos.validate_config(config)


def test_live_execution_requires_confirmation_before_commands(monkeypatch):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(side_effect=AssertionError("external call")))
    with pytest.raises(chaos.Blocked, match="confirmation"):
        chaos.main(["--execute"])


@pytest.mark.parametrize("report_args,message", [
    ([], "--report required"),
    (["--report", str(Path("..") / "outside-chaos-report.json")], "inside current working directory"),
    (["--report", "."], "Report must be new"),
])
def test_live_report_preflight_fails_before_any_cluster_call(monkeypatch, report_args, message):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(side_effect=AssertionError("external call")))
    with pytest.raises(chaos.Blocked, match=message):
        chaos.main(["--execute", "--confirm", chaos.CONFIRMATION, *report_args])


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired(["kubectl", "secret"], 1, output="Bearer private"),
    FileNotFoundError("private-credential"),
])
def test_dependency_unavailable_or_timeout_redacts_output(monkeypatch, error):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        chaos.Commands().run(["kubectl"], label="dependency")
    assert "private" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_dependency_nonzero_redacts_output(monkeypatch):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="Bearer secret", stderr="connection credentials",
    )))
    with pytest.raises(RuntimeError, match="output suppressed") as raised:
        chaos.Commands().run(["kubectl"])
    assert "secret" not in str(raised.value)


def test_windows_cmd_entrypoint_resolved_without_shell(monkeypatch):
    monkeypatch.setattr(chaos.shutil, "which", lambda _: r"C:\AzureCLI\az.cmd")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="{}"))
    monkeypatch.setattr(chaos.subprocess, "run", run)
    chaos.Commands().run(["az", "aks", "show"])
    assert run.call_args.args[0] == [r"C:\AzureCLI\az.cmd", "aks", "show"]
    assert not run.call_args.kwargs.get("shell", False)


def deployment(replicas=2):
    return {
        "metadata": {"generation": 5}, "spec": {"replicas": replicas},
        "status": {"observedGeneration": 5, "replicas": replicas,
                   "updatedReplicas": replicas, "readyReplicas": replicas,
                   "availableReplicas": replicas},
    }


@pytest.mark.parametrize("field,value", [
    ("observedGeneration", 4), ("updatedReplicas", 1), ("replicas", 3),
    ("readyReplicas", 1), ("availableReplicas", 1), ("unavailableReplicas", 1),
])
def test_actual_restoration_not_merely_desired_replicas(field, value):
    value_deployment = deployment()
    value_deployment["status"][field] = value
    assert not chaos.healthy(value_deployment, 2)
    assert chaos.healthy(deployment(), 2)
    assert chaos.healthy(deployment(0), 0)


@pytest.mark.parametrize("fault", [
    "cluster_identity", "foreign_server", "insecure_tls", "hpa",
    "unhealthy_worker", "stale_signal",
])
def test_readonly_preflight_blocks_wrong_cluster_or_unhealthy_baseline(config, monkeypatch, fault):
    harness = chaos.Harness(config)
    cluster = {
        "id": "/subscriptions/" + config["subscription"] + "/resourceGroups/" +
              config["resource_group"] + "/providers/Microsoft.ContainerService/managedClusters/" +
              config["cluster"],
        "fqdn": "isolated.aks.invalid",
    }
    context = {"clusters": [{"cluster": {"server": "https://isolated.aks.invalid"}}]}
    hpas = {"items": []}
    worker = deployment()
    if fault == "cluster_identity":
        cluster["id"] = "/subscriptions/foreign/resourceGroups/customer"
    elif fault == "foreign_server":
        context["clusters"][0]["cluster"]["server"] = "https://customer.aks.invalid"
    elif fault == "insecure_tls":
        context["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    elif fault == "hpa":
        hpas["items"] = [{"spec": {"scaleTargetRef": {"name": chaos.WORKER}}}]
    elif fault == "unhealthy_worker":
        worker["status"]["readyReplicas"] = 1
    else:
        worker["metadata"]["annotations"] = {chaos.SIGNAL_RUN: "previous-run"}
    monkeypatch.setattr(chaos.Path, "is_file", lambda _: True)
    monkeypatch.setattr(harness, "command", Mock(return_value=json.dumps(cluster)))
    kube = Mock(side_effect=lambda *args, **kwargs: json.dumps(context if args[0] == "config" else hpas))
    monkeypatch.setattr(harness, "kube", kube)
    monkeypatch.setattr(harness, "get", Mock(return_value=worker))
    baseline_probe = Mock(side_effect=AssertionError("baseline must not run"))
    monkeypatch.setattr(harness, "probe", baseline_probe)
    with pytest.raises(chaos.Blocked):
        harness.preflight()
    assert all(call.args[0] in ("config", "get") for call in kube.call_args_list)
    baseline_probe.assert_not_called()


def simulated_harness(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.originals = dict(chaos.DEPLOYMENTS)
    baseline = {"snapshot": [], "identity": {"model": config["model_id"]}}

    def baseline_preflight():
        harness.baseline = baseline

    def acquire():
        harness.locked = True

    def establish():
        harness.watchdog_possible = True

    def inject():
        harness.outage_possible = True
        harness.fixture_possible = True

    def restore():
        harness.watchdog_possible = False

    def run_probe(action):
        return baseline if action == "baseline" else (
            baseline["identity"] if action == "registry" else {"passed": True}
        )

    for name, replacement in {
        "preflight": baseline_preflight, "acquire": acquire, "all_healthy": lambda: True,
        "phase": lambda *_: None, "establish_watchdog": establish, "inject": inject,
        "restore": restore, "probe": run_probe, "release_lock": lambda: None,
    }.items():
        monkeypatch.setattr(harness, name, Mock(side_effect=replacement))
    return harness


def test_success_reports_harness_not_conversational_repair(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    report = harness.execute()
    assert report["result"] == "passed"
    assert report["autonomous_conversational_repair"].startswith("blocked")
    assert report["processing_recovery"].startswith("passed")
    harness.restore.assert_called_once()
    assert "cleanup" in [call.args[0] for call in harness.probe.call_args_list]


def test_unavailable_baseline_never_acquires_lock_or_injects(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    harness.preflight.side_effect = RuntimeError("dependency unavailable secret")
    report = harness.execute()
    assert report["result"] == "blocked"
    assert "secret" not in json.dumps(report)
    harness.acquire.assert_not_called()
    harness.inject.assert_not_called()


def test_concurrent_run_lock_prevents_watchdog_and_outage(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    harness.acquire.side_effect = RuntimeError("AlreadyExists")
    assert harness.execute()["result"] == "blocked"
    harness.establish_watchdog.assert_not_called()
    harness.inject.assert_not_called()


def test_outage_timeout_still_restores_and_cleans_only_owned_fixture(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)

    def timeout():
        harness.outage_possible = harness.fixture_possible = True
        raise TimeoutError("queue unavailable secret")

    harness.inject.side_effect = timeout
    report = harness.execute()
    assert report["result"] == "failed"
    assert "secret" not in json.dumps(report)
    harness.restore.assert_called_once()
    assert "cleanup" in [call.args[0] for call in harness.probe.call_args_list]
    assert report["processing_recovery"] == "not_checked"


def test_failed_restoration_retains_watchdog_lock_and_fixture(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    harness.restore.side_effect = TimeoutError("control plane unavailable")
    report = harness.execute()
    assert report["result"] == "failed" and harness.watchdog_possible
    assert report["readiness"].startswith("restoration_unverified")
    harness.release_lock.assert_not_called()
    assert "cleanup" not in [call.args[0] for call in harness.probe.call_args_list]


def test_processing_assertion_still_cleans_owned_fixture(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    original = harness.probe.side_effect

    def fail_verify(action):
        if action == "verify":
            raise AssertionError("duplicates")
        return original(action)

    harness.probe.side_effect = fail_verify
    report = harness.execute()
    assert report["result"] == "failed" and report["cleanup"] == {"passed": True}
    harness.restore.assert_called_once()


def test_eventgrid_cleanup_failure_retains_lock_and_reports_failed(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    original = harness.probe.side_effect

    def missing_deletion(action):
        if action == "cleanup":
            raise TimeoutError("EventGrid unavailable")
        return original(action)

    harness.probe.side_effect = missing_deletion
    report = harness.execute()
    assert report["result"] == "failed"
    assert report["processing_recovery"].startswith("passed")
    assert report["cleanup"] == "failed_owned_fixture_only; lock_retained"
    harness.release_lock.assert_not_called()


def test_injection_orders_watchdog_arm_outage_owned_queue_then_restarts(config, monkeypatch):
    harness = chaos.Harness(config)
    events = []
    monkeypatch.setattr(harness, "phase", lambda phase: events.append("phase:" + phase))
    monkeypatch.setattr(harness, "get", lambda *args: deployment(0))
    monkeypatch.setattr(harness, "kube", lambda *args, **kwargs: events.append("kube:" + " ".join(args)))
    monkeypatch.setattr(harness, "probe", lambda action: events.append("probe:" + action) or {"owned": True})
    harness.inject()
    assert events[:4] == [
        "phase:armed", "kube:scale deployment/" + chaos.WORKER + " --replicas=0",
        "probe:upload", "probe:queued",
    ]
    assert events[4:] == ["kube:rollout restart deployment/" + name for name in chaos.RESTARTS]
    assert harness.report["observed_failure"][0]["failure"] == "owned_work_queued_without_processing"
    assert harness.report["repair_actor"] == []


def test_watchdog_recovery_before_observation_blocks_restart_claim(config, monkeypatch):
    harness = chaos.Harness(config)
    monkeypatch.setattr(harness, "phase", lambda _: None)
    monkeypatch.setattr(harness, "get", Mock(side_effect=[deployment(0), deployment(2)]))
    kube = Mock()
    monkeypatch.setattr(harness, "kube", kube)
    monkeypatch.setattr(harness, "probe", Mock(return_value={"owned": True}))
    with pytest.raises(chaos.Blocked, match="restored before queue observation"):
        harness.inject()
    assert [call.args[0] for call in kube.call_args_list] == ["scale"]
    assert harness.report["observed_failure"] == []


def test_release_never_deletes_other_runs_lock_or_annotations(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.locked = harness.signal_possible = True
    monkeypatch.setattr(harness, "get", Mock(return_value={"data": {"run_id": "someone-else"}}))
    kube = Mock()
    monkeypatch.setattr(harness, "kube", kube)
    with pytest.raises(chaos.Blocked, match="another run"):
        harness.release_lock()
    kube.assert_not_called()
    assert harness.locked


def test_release_uses_resource_version_and_only_own_signal_keys(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.locked = harness.signal_possible = True
    monkeypatch.setattr(harness, "get", Mock(side_effect=[
        {"data": {"run_id": harness.run_id}},
        {"metadata": {"resourceVersion": "captured-revision", "annotations": {
            chaos.SIGNAL_RUN: harness.run_id, chaos.SIGNAL_PHASE: "restored", "unrelated": "keep",
        }}},
    ]))
    kube = Mock()
    monkeypatch.setattr(harness, "kube", kube)
    harness.release_lock()
    args = kube.call_args_list[0].args
    assert args[:3] == ("patch", "deployment", chaos.WORKER)
    assert json.loads(args[-1]) == {"metadata": {
        "resourceVersion": "captured-revision",
        "annotations": {chaos.SIGNAL_RUN: None, chaos.SIGNAL_PHASE: None},
    }}
    assert kube.call_args_list[1].args[:3] == ("delete", "configmap", chaos.LOCK)
    assert not harness.locked

def test_watchdog_not_ready_never_injects(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)

    def unavailable_watchdog():
        harness.watchdog_possible = True
        raise RuntimeError("RBAC denied")

    harness.establish_watchdog.side_effect = unavailable_watchdog
    assert harness.execute()["result"] == "blocked"
    harness.inject.assert_not_called()
    harness.restore.assert_called_once()


def test_restore_deletes_watchdog_only_after_all_health_and_ack(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.originals = dict(chaos.DEPLOYMENTS)
    calls = []
    monkeypatch.setattr(harness, "phase", lambda value: calls.append(("phase", value)))
    monkeypatch.setattr(harness, "all_healthy", lambda: True)
    monkeypatch.setattr(harness, "kube", lambda *args, **kw: (
        calls.append(args) or ("WATCHDOG_RESTORE_ATTEMPT\nWATCHDOG_RESTORED" if args[0] == "logs" else "")
    ))
    harness.watchdog_possible = True
    harness.restore()
    assert calls[-1][:2] == ("delete", "job")
    assert ("phase", "restored") in calls
    assert harness.report["repair_actor"] == [
        "harness_finally_replica_restore", "independent_kubernetes_watchdog",
    ]
    assert not harness.watchdog_possible


def test_restore_does_not_delete_unacknowledged_watchdog(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.originals = dict(chaos.DEPLOYMENTS)
    calls = []
    monkeypatch.setattr(harness, "phase", lambda *_: None)
    monkeypatch.setattr(harness, "all_healthy", lambda: True)
    monkeypatch.setattr(harness, "kube", lambda *args, **kw: calls.append(args) or "")
    harness.watchdog_possible = True
    with pytest.raises(chaos.Blocked, match="acknowledged"):
        harness.restore()
    assert harness.watchdog_possible
    assert not any(call[0] == "delete" for call in calls)


def test_restore_does_not_delete_watchdog_when_actual_readiness_times_out(config, monkeypatch):
    harness = chaos.Harness(config)
    harness.originals = dict(chaos.DEPLOYMENTS)
    kube = Mock(return_value="")
    monkeypatch.setattr(harness, "kube", kube)
    monkeypatch.setattr(harness, "pause_until", Mock(side_effect=TimeoutError("readiness")))
    harness.watchdog_possible = True
    with pytest.raises(TimeoutError):
        harness.restore()
    assert harness.watchdog_possible
    assert [call.args[0] for call in kube.call_args_list] == ["scale"]


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_probe_deadline_is_bounded():
    clock = Clock()
    with pytest.raises(RuntimeError, match="deadline"):
        probe.wait_until(lambda: None, 7, sleep=clock.sleep, clock=clock)
    assert clock.now == 7


def test_probe_rejects_duplicates_and_bad_vectors(config):
    rows = [{"id": ref, "source_ref": ref, "source_id": config["source_id"],
             "embedding": [0.1] * 1536} for ref in config["expected_refs"]]
    probe.validate_rows(rows, config)
    with pytest.raises(RuntimeError, match="Duplicate"):
        probe.validate_rows(rows + [rows[0]], config)
    rows[0]["embedding"][0] = float("nan")
    with pytest.raises(RuntimeError, match="embedding"):
        probe.validate_rows(rows, config)


@pytest.mark.parametrize("arm_lost", [False, True])
def test_independent_watchdog_restores_after_host_loss_and_transient_failure(arm_lost):
    clock = Clock()
    settings = {
        "namespace": "omnivec", "worker": chaos.WORKER, "replicas": 2,
        "lock": chaos.LOCK, "run_id": "owned", "arm_timeout": 4,
        "outage_seconds": 6, "restore_seconds": 20,
    }
    apps = Mock()
    apps.patch_namespaced_deployment.side_effect = [None, TimeoutError("unavailable"), None]
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(generation=2, annotations={
            chaos.SIGNAL_RUN: "owned", chaos.SIGNAL_PHASE: "preflight" if arm_lost else "armed",
        }), spec=SimpleNamespace(replicas=2),
        status=SimpleNamespace(observed_generation=2, replicas=2, updated_replicas=2,
                               ready_replicas=2, available_replicas=2),
    )
    output = []
    watchdog.watch(apps, settings, clock=clock, sleep=clock.sleep,
                   emit=lambda value, **_: output.append(value))
    assert output == ["WATCHDOG_READY", "WATCHDOG_RESTORE_ATTEMPT", "WATCHDOG_RESTORED"]
    assert apps.patch_namespaced_deployment.call_args_list[0].kwargs == {"dry_run": "All"}
    assert clock.now <= 26


def test_watchdog_fails_closed_when_dry_run_denied():
    apps = Mock()
    apps.patch_namespaced_deployment.side_effect = RuntimeError("RBAC denied")
    output = []
    with pytest.raises(RuntimeError, match="denied"):
        watchdog.watch(apps, {
            "namespace": "omnivec", "worker": chaos.WORKER, "replicas": 2,
            "lock": chaos.LOCK, "run_id": "owned",
        }, emit=lambda value, **_: output.append(value))
    assert output == []


def test_watchdog_persistent_control_plane_failure_is_bounded():
    clock = Clock()
    apps = Mock()
    apps.patch_namespaced_deployment.side_effect = [None] + [TimeoutError("unavailable")] * 20
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(annotations={
            chaos.SIGNAL_RUN: "owned", chaos.SIGNAL_PHASE: "armed",
        }),
    )
    output = []
    with pytest.raises(RuntimeError, match="restoration unverified"):
        watchdog.watch(apps, {
            "namespace": "omnivec", "worker": chaos.WORKER, "replicas": 2,
            "run_id": "owned", "arm_timeout": 4, "outage_seconds": 6, "restore_seconds": 10,
        }, clock=clock, sleep=clock.sleep, emit=lambda value, **_: output.append(value))
    assert clock.now == 16
    assert output == ["WATCHDOG_READY", "WATCHDOG_RESTORE_ATTEMPT"]


def test_watchdog_disarms_only_when_actual_replicas_restored():
    clock = Clock()
    apps = Mock()
    apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(generation=2, annotations={
            chaos.SIGNAL_RUN: "owned", chaos.SIGNAL_PHASE: "restored",
        }), spec=SimpleNamespace(replicas=2),
        status=SimpleNamespace(observed_generation=2, replicas=2, updated_replicas=2,
                               ready_replicas=2, available_replicas=2),
    )
    output = []
    watchdog.watch(apps, {
        "namespace": "omnivec", "worker": chaos.WORKER, "replicas": 2,
        "run_id": "owned", "arm_timeout": 4, "outage_seconds": 6, "restore_seconds": 10,
    }, clock=clock, sleep=clock.sleep, emit=lambda value, **_: output.append(value))
    assert output == ["WATCHDOG_READY", "WATCHDOG_DISARMED"]
    assert apps.patch_namespaced_deployment.call_count == 1
