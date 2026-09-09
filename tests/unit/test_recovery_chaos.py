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
    assert report["autonomous_conversational_repair"] == "not_exercised_by_base_harness"
    assert report["diagnostic_recovery_tools"] == "not_integrated_by_base_harness"
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


def test_command_failure_keeps_returncode_and_safe_stderr_cause(monkeypatch):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=27, stdout="credential=private",
        stderr='Unable to connect to the server: Get "https://private.invalid?sig=private": '
               'dial tcp 192.0.2.10:443: i/o timeout\nAuthorization: Bearer private\n',
    )))
    with pytest.raises(chaos.CommandFailure) as raised:
        chaos.Commands().run(["kubectl"], label="kubectl get")
    evidence = raised.value.evidence
    assert evidence["returncode"] == 27 and evidence["classification"] == "timeout"
    assert "i/o timeout" in evidence["stderr_excerpt"]
    assert "unable to connect" in evidence["stderr_excerpt"]
    for secret in ("private", "192.0.2.10", "Authorization", "sig="):
        assert secret not in json.dumps(evidence)


@pytest.mark.parametrize("stderr,classification,excerpt", [
    ("Error from server (Forbidden): secret", "authorization", "forbidden"),
    ("You must be logged in to the server (Unauthorized) token=private", "authentication", "unauthorized"),
    ("x509: certificate signed by unknown authority private", "tls_certificate", "certificate"),
    ('Error from server (NotFound): pods "private" not found', "not_found", "not found"),
    ('Error from server (AlreadyExists): private already exists', "already_exists", "already exists"),
    ("Error from server (Too Many Requests): private", "throttled", "throttling"),
    ("connection reset by peer https://private", "transport", "connection refused/reset/aborted"),
    ("lookup private: no such host", "transport", "DNS"),
    ("http2: server sent GOAWAY private", "transport", "transport closed"),
    ("ServiceUnavailable private", "service_unavailable", "service unavailable"),
])
def test_limited_stderr_classification_never_copies_arbitrary_values(stderr, classification, excerpt):
    evidence = chaos.CommandFailure("kubectl get", returncode=1, stderr=stderr).evidence
    assert evidence["classification"] == classification
    assert excerpt in evidence["stderr_excerpt"]
    assert "private" not in json.dumps(evidence)


def test_unknown_stderr_and_stdout_never_escape_into_report():
    error = chaos.CommandFailure("pod probe verify", returncode=3,
                                 stderr="opaque-unlabelled-credential\n" * 10000,
                                 stdout='{"api_key":"private"}')
    assert error.evidence["stderr_excerpt"] == "[unrecognized stderr omitted]"
    assert len(json.dumps(error.evidence)) < 500
    assert "credential" not in json.dumps(error.evidence)


def test_safe_pod_error_type_survives_without_logging_stdout(monkeypatch):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stderr="command terminated with exit code 1",
        stdout="private response\nCHAOS_ERROR=ClientAuthenticationError\n",
    )))
    with pytest.raises(chaos.CommandFailure) as raised:
        chaos.Commands().run(["kubectl"], label="pod probe verify")
    assert raised.value.evidence["probe_error_type"] == "ClientAuthenticationError"
    assert raised.value.evidence["classification"] == "pod_probe_error"
    assert "private" not in json.dumps(raised.value.evidence)


def test_timeout_has_null_returncode_and_redacts_partial_output(monkeypatch):
    monkeypatch.setattr(chaos.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(
        ["kubectl"], 10, output=b"private", stderr=b"TLS handshake timeout https://private",
    )))
    with pytest.raises(chaos.CommandFailure) as raised:
        chaos.Commands().run(["kubectl"], label="kubectl get")
    assert raised.value.evidence["returncode"] is None
    assert raised.value.evidence["classification"] == "timeout"
    assert raised.value.evidence["process_timed_out"] is True
    assert raised.value.evidence["stderr_excerpt"] == "TLS handshake timeout"
    assert "private" not in json.dumps(raised.value.evidence)


def test_get_retries_only_transport_failures_then_records_recovery(config):
    clock = Clock()
    commands = Mock()
    commands.run.side_effect = [
        chaos.CommandFailure("kubectl get", classification="timeout"),
        chaos.CommandFailure("kubectl get", returncode=1, stderr="connection reset by peer private"),
        '{"items":[]}',
    ]
    harness = chaos.Harness(config, commands=commands, clock=clock, sleep=clock.sleep)
    harness.stage = "processing_verification"
    assert harness.kube("get", "pods", "-o", "json") == '{"items":[]}'
    assert commands.run.call_count == 3 and clock.now == 3
    failures = harness.report["read_attempt_failures"]
    assert [e["attempt"] for e in failures] == [1, 2]
    assert all(e["will_retry"] and e["stage"] == "processing_verification" for e in failures)
    assert "primary_failure" not in harness.report


@pytest.mark.parametrize("stderr", [
    "Forbidden", "Unauthorized", "x509: bad certificate", "NotFound", "AlreadyExists",
    "Too Many Requests", "ServiceUnavailable", "unrecognized semantic error",
])
def test_get_nontransport_errors_never_retry(config, stderr):
    commands = Mock()
    commands.run.side_effect = chaos.CommandFailure("kubectl get", returncode=1, stderr=stderr)
    harness = chaos.Harness(config, commands=commands, sleep=Mock(side_effect=AssertionError("retry")))
    with pytest.raises(chaos.CommandFailure):
        harness.kube("get", "pods", "-o", "json")
    commands.run.assert_called_once()
    assert harness.report["read_attempt_failures"][0]["will_retry"] is False


def test_authentication_evidence_prevents_retry_even_if_process_also_timed_out(config):
    commands = Mock()
    commands.run.side_effect = chaos.CommandFailure(
        "kubectl get", stderr="Unauthorized private", classification="timeout",
    )
    harness = chaos.Harness(config, commands=commands, sleep=Mock(side_effect=AssertionError("retry")))
    with pytest.raises(chaos.CommandFailure):
        harness.kube("get", "pods", "-o", "json")
    commands.run.assert_called_once()
    evidence = harness.report["read_attempt_failures"][0]
    assert evidence["classification"] == "authentication" and evidence["process_timed_out"] is True


@pytest.mark.parametrize("args", [
    ("scale", "deployment/worker", "--replicas=2"), ("rollout", "restart", "deployment/api"),
    ("create", "-f", "-"), ("patch", "deployment", "worker"), ("replace", "-f", "-"),
    ("delete", "job", "watchdog"), ("apply", "-f", "-"),
    ("exec", "api-pod", "--", "python3", "-"), ("logs", "pod/api"),
    ("wait", "--for=condition=Ready", "pod/api"), ("config", "view"),
    ("list", "pods"), ("get", "pods", "--raw=/api/v1"), ("get", "pods", "--watch"),
    ("get", "pods", "-w=true"), ("get", "unknown-extension"), ("get", "pods/api/log"),
])
def test_ambiguous_writes_exec_and_other_operations_never_retry(config, args):
    commands = Mock()
    commands.run.side_effect = chaos.CommandFailure("kubectl " + args[0], classification="timeout")
    harness = chaos.Harness(config, commands=commands, sleep=Mock(side_effect=AssertionError("retry")))
    with pytest.raises(chaos.CommandFailure):
        harness.kube(*args)
    commands.run.assert_called_once()
    assert "read_attempt_failures" not in harness.report


def test_get_with_stdin_document_is_not_assumed_retryable(config):
    commands = Mock()
    commands.run.side_effect = chaos.CommandFailure("kubectl get", classification="timeout")
    harness = chaos.Harness(config, commands=commands, sleep=Mock(side_effect=AssertionError("retry")))
    with pytest.raises(chaos.CommandFailure):
        harness.kube("get", "pods", document={"unsafe": "do_not_assume"})
    commands.run.assert_called_once()


def test_get_retry_budget_includes_backoff_and_all_attempts(config):
    clock = Clock()
    commands = Mock()

    def timeout(_args, **kwargs):
        clock.sleep(kwargs["timeout"])
        raise chaos.CommandFailure("kubectl get", classification="timeout")

    commands.run.side_effect = timeout
    harness = chaos.Harness(config, commands=commands, clock=clock, sleep=clock.sleep)
    with pytest.raises(chaos.CommandFailure):
        harness.kube("get", "deployments", "-o", "json", timeout=30)
    assert clock.now == 30 and commands.run.call_count == 3
    assert [call.kwargs["timeout"] for call in commands.run.call_args_list] == [10, 10, 7]
    assert [e["will_retry"] for e in harness.report["read_attempt_failures"]] == [True, True, False]


def test_get_retry_budget_never_exceeds_remaining_harness_deadline(config):
    clock = Clock()
    commands = Mock()

    def timeout(_args, **kwargs):
        clock.sleep(kwargs["timeout"])
        raise chaos.CommandFailure("kubectl get", classification="timeout")

    commands.run.side_effect = timeout
    harness = chaos.Harness(config, commands=commands, clock=clock, sleep=clock.sleep)
    harness.deadline = 15
    with pytest.raises(chaos.CommandFailure):
        harness.kube("get", "pods", timeout=30)
    assert clock.now == 15 and commands.run.call_count == 2


def test_get_json_parse_failure_is_not_retried(config):
    commands = Mock()
    commands.run.return_value = "{invalid"
    harness = chaos.Harness(config, commands=commands)
    with pytest.raises(json.JSONDecodeError):
        harness.get("deployment", "worker")
    commands.run.assert_called_once()


def test_primary_and_cleanup_failures_preserve_distinct_stages_and_returncodes(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    original = harness.probe.side_effect

    def fail(action):
        if action == "verify":
            harness.report["last_operation"] = "pod probe verify"
            raise chaos.CommandFailure("pod probe verify", returncode=17,
                                       stderr="connection reset by peer private")
        if action == "cleanup":
            harness.report["last_operation"] = "kubectl get"
            raise chaos.CommandFailure("kubectl get", classification="timeout",
                                       stderr="TLS handshake timeout private")
        return original(action)

    harness.probe.side_effect = fail
    report = harness.execute()
    assert report["result"] == "failed"
    primary, cleanup = report["primary_failure"], report["cleanup_failure"]
    assert primary["stage"] == "processing_verification" and primary["operation"] == "pod probe verify"
    assert primary["returncode"] == 17 and primary["classification"] == "transport"
    assert cleanup["stage"] == "fixture_cleanup" and cleanup["operation"] == "kubectl get"
    assert cleanup["returncode"] is None and cleanup["classification"] == "timeout"
    assert "private" not in json.dumps(report)
    harness.release_lock.assert_not_called()


def test_restoration_failure_does_not_overwrite_primary_injection_failure(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    harness.inject.side_effect = chaos.CommandFailure("kubectl rollout", returncode=4, stderr="Forbidden")
    harness.restore.side_effect = chaos.CommandFailure("kubectl scale", classification="timeout")
    report = harness.execute()
    assert report["primary_failure"]["stage"] == "fault_injection"
    assert report["primary_failure"]["returncode"] == 4
    assert report["restoration_failure"]["stage"] == "restoration"
    assert report["restoration_failure"]["classification"] == "timeout"


def test_lock_cleanup_failure_has_its_own_stage(config, monkeypatch):
    harness = simulated_harness(config, monkeypatch)
    harness.release_lock.side_effect = chaos.CommandFailure("kubectl delete", returncode=9, stderr="Forbidden")
    report = harness.execute()
    assert report["result"] == "failed" and report["processing_recovery"].startswith("passed")
    assert report["lock_cleanup_failure"]["stage"] == "lock_cleanup"
    assert report["lock_cleanup_failure"]["returncode"] == 9
    assert "primary_failure" not in report


@pytest.mark.parametrize("action", ["baseline", "registry", "upload", "queued", "verify", "cleanup", "search"])
def test_pod_probe_transfers_script_over_stdin_not_large_command_argument(config, monkeypatch, action):
    commands = Mock()
    commands.run.return_value = 'CHAOS_PROBE={"fixture":"offline"}\n'
    harness = chaos.Harness(config, commands=commands)
    monkeypatch.setattr(harness, "kube", Mock(return_value=json.dumps({"items": [{
        "metadata": {"name": "ready-api"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }]})))
    assert harness.probe(action) == {"fixture": "offline"}
    commands.run.assert_called_once()
    args = commands.run.call_args.args[0]
    options = commands.run.call_args.kwargs
    assert args[-4:-1] == ["python3", "-", action]
    payload = json.loads(chaos.base64.b64decode(args[-1]))
    assert payload == {"config": config, "run_id": harness.run_id, "baseline": None}
    code = (SCRIPTS / "recovery_chaos_probe.py").read_text(encoding="utf-8")
    assert options["stdin"] == code
    assert code not in args and "-c" not in args
    assert options["timeout"] == 155 and options["label"] == "pod probe " + action
    assert len(" ".join(args)) < len(code)


@pytest.mark.parametrize("action", ["registry", "cleanup"])
def test_stdin_exec_failure_is_not_retried_even_for_readonly_probe(config, monkeypatch, action):
    commands = Mock()
    commands.run.side_effect = chaos.CommandFailure("pod probe " + action, classification="timeout")
    harness = chaos.Harness(config, commands=commands)
    monkeypatch.setattr(harness, "kube", Mock(return_value=json.dumps({"items": [{
        "metadata": {"name": "ready-api"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }]})))
    with pytest.raises(chaos.CommandFailure):
        harness.probe(action)
    commands.run.assert_called_once()
    assert commands.run.call_args.kwargs["stdin"]
    assert "read_attempt_failures" not in harness.report
