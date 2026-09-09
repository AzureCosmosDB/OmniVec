"""Offline tests of the separate legitimate approved-agent-resume harness."""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pod = load("agent_recovery_test_probe", "agent_recovery_probe.py")
host = load("agent_recovery_test_host", "agent-recovery-scenario.py")
PID, SID, CALL, SESSION = "pip-owned", "src-owned", "call-owned", "session-owned"
PREFIX = "pr183-agent-recovery-" + "a" * 32


def proposal_events():
    return [
        {"type": "session", "session_id": SESSION},
        {"type": "tool_call", "id": "diagnosis", "name": "diagnose_pipeline", "args": {"pipeline_id": PID}},
        {"type": "tool_result", "id": "diagnosis", "name": "diagnose_pipeline", "result": {
            "status": "BLOCKED", "unknown": [], "findings": [{"code": "paused"}],
            "pipelines": [{"pipeline_id": PID, "unknown": []}],
        }},
        {"type": "approval_required", "tool": "resume_pipeline", "call_id": CALL, "args": {"pipeline_id": PID}},
        {"type": "done"},
    ]


def pending():
    return [{"session_id": SESSION, "call_id": CALL, "tool_name": "resume_pipeline",
             "args": {"pipeline_id": PID}}]


def approval_events():
    now = datetime.now(timezone.utc)
    record = {
        "tool": "resume_pipeline", "scope": PID, "attempt": 1, "max_actions": 2,
        "outcome": "VERIFIED_PROCESSING", "verification": {
            "status": "HEALTHY", "processing_verified": True, "unknown": [],
            "baseline_at": (now - timedelta(seconds=10)).isoformat(),
            "observed_at": now.isoformat(),
            "pipelines": [{"pipeline_id": PID, "status": "HEALTHY", "processing_verified": True,
                           "progress": {"embedded_before": 0, "embedded_after": 1}}],
        },
    }
    return [
        {"type": "approval_decision", "call_id": CALL, "decision": "approve", "tool": "resume_pipeline"},
        {"type": "tool_result", "id": CALL, "name": "resume_pipeline", "result": {"success": True, "recovery": record}},
        {"type": "verification", "recovery": record},
        {"type": "final", "text": "Untrusted natural-language explanation", "recovery": record},
        {"type": "done"},
    ]


def test_default_scenario_plan_makes_no_external_calls(monkeypatch, capsys):
    monkeypatch.setattr(host.subprocess, "run", Mock(side_effect=AssertionError("external call")))
    assert host.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["external_calls"] == 0
    assert report["base_worker_restart_chaos"] == "unchanged_and_not_executed"
    assert report["manual_resume"] == "prohibited"


def test_separate_authorization_required(monkeypatch):
    monkeypatch.setattr(host, "execute", Mock(side_effect=AssertionError("live execution")))
    with pytest.raises(RuntimeError, match="Separate parent"):
        host.main(["--execute", "--confirm", "ISOLATED-PR183-CHAOS-AUTHORIZED"])


@pytest.mark.parametrize("prefix", [
    "pr183-chunks-old", "customer", PREFIX + "-extra", "pr183-agent-recovery-../escape",
])
def test_new_owned_prefix_is_mandatory(prefix):
    with pytest.raises(pod.Blocked):
        pod.prefix_valid(prefix)


def test_sse_actual_proxy_data_contract():
    events = proposal_events()
    lines = []
    for event in events:
        lines += [("data: " + json.dumps(event)).encode(), b""]
    assert pod.parse_sse(lines) == events


@pytest.mark.parametrize("lines,code", [
    ([b'data: {"type":"error","detail":"private-token"}', b""], "agent_or_proxy_error"),
    ([b'data: {"type":"token","text":"success"}', b""], "sse_missing_done"),
    ([b"data: []", b""], "invalid_sse_event"),
    ([b"data: " + b"x" * 2_000_001], "sse_size_limit"),
])
def test_sse_error_partial_or_unbounded_stream_never_proves_success(lines, code):
    with pytest.raises(pod.Blocked, match=code) as error:
        pod.parse_sse(lines)
    assert "private-token" not in str(error.value)


def test_sse_deadline():
    with pytest.raises(pod.Blocked, match="sse_deadline"):
        pod.parse_sse([b"data: {}"], clock=lambda: 10, deadline=9)


def test_one_scoped_diagnosed_resume_proposal_selected():
    assert pod.select_proposal(proposal_events(), PID) == (SESSION, CALL)
    pod.validate_pending(pending(), SESSION, CALL, PID)


@pytest.mark.parametrize("tool", ["scale_deployment", "restart_pod", "purge_dlq", "reset_pipeline_offsets", "pause_pipeline"])
def test_other_agent_mutations_never_approved(tool):
    events = proposal_events()
    events[3]["tool"] = tool
    with pytest.raises(pod.Blocked, match="non_allowlisted_action"):
        pod.select_proposal(events, PID)


@pytest.mark.parametrize("args", [
    {"pipeline_id": "original-pipeline"}, {"pipeline_id": PID, "force": True},
    {"pipeline_id": PID, "other_ids": ["foreign"]}, {},
])
def test_proposal_scope_must_be_exact_no_extra_arguments(args):
    events = proposal_events()
    events[3]["args"] = args
    with pytest.raises(pod.Blocked, match="action_scope"):
        pod.select_proposal(events, PID)


@pytest.mark.parametrize("fault", [
    "no_diagnosis", "wrong_scope", "unknown_identity", "shared_dlq", "healthy_snapshot",
    "multiple_proposals", "foreign_result",
])
def test_diagnosis_must_prove_only_owned_pause_before_any_approval(fault):
    events = proposal_events()
    if fault == "no_diagnosis":
        events.pop(2)
    elif fault == "wrong_scope":
        events[1]["args"]["pipeline_id"] = "foreign"
    elif fault == "unknown_identity":
        events[2]["result"]["unknown"] = ["AADSTS700213"]
    elif fault == "shared_dlq":
        events[2]["result"]["findings"].append({"code": "dlq"})
    elif fault == "healthy_snapshot":
        events[2]["result"]["status"] = "READY_IDLE"
    elif fault == "multiple_proposals":
        events.insert(4, copy.deepcopy(events[3]))
    else:
        events[2]["result"]["pipelines"][0]["pipeline_id"] = "foreign"
    with pytest.raises(pod.Blocked):
        pod.select_proposal(events, PID)


@pytest.mark.parametrize("fault", ["session", "call", "tool", "scope", "multiple", "missing"])
def test_server_pending_record_revalidated_not_just_model_event(fault):
    values = pending()
    if fault == "session":
        values[0]["session_id"] = "other"
    elif fault == "call":
        values[0]["call_id"] = "other"
    elif fault == "tool":
        values[0]["tool_name"] = "purge_dlq"
    elif fault == "scope":
        values[0]["args"]["pipeline_id"] = "other"
    elif fault == "multiple":
        values *= 2
    else:
        values = []
    with pytest.raises(pod.Blocked):
        pod.validate_pending(values, SESSION, CALL, PID)


def test_approved_runtime_action_with_actual_observations_accepted():
    proof = pod.recovery_proof(approval_events(), CALL, PID)
    assert proof["outcome"] == "VERIFIED_PROCESSING"
    assert proof["embedded_before"] == 0 and proof["embedded_after"] == 1


@pytest.mark.parametrize("outcome", [
    "READY_IDLE", "NOT_VERIFIED", "ACTION_FAILED", "ACTION_NOT_EXECUTED", "UNKNOWN",
])
def test_other_runtime_outcomes_never_count_as_agent_repair(outcome):
    events = approval_events()
    events[1]["result"]["recovery"]["outcome"] = outcome
    with pytest.raises(pod.Blocked, match="runtime_processing_not_verified"):
        pod.recovery_proof(events, CALL, PID)


@pytest.mark.parametrize("fault", [
    "denied", "wrong_call", "wrong_tool", "wrong_scope", "second_attempt",
    "processing_false", "unknown", "no_timestamps", "same_timestamp", "stale",
    "same_count", "boolean_count", "foreign_progress", "no_final", "no_verification",
    "another_proposal", "action_error", "missing_action_success", "nested_unknown", "boolean_attempt",
])
def test_unproven_or_unscoped_runtime_evidence_rejected(fault):
    events = approval_events()
    record = events[1]["result"]["recovery"]
    verification = record["verification"]
    if fault == "denied":
        events[0]["decision"] = "deny"
    elif fault == "wrong_call":
        events[0]["call_id"] = "foreign"
    elif fault == "wrong_tool":
        record["tool"] = "verify_recovery"
    elif fault == "wrong_scope":
        record["scope"] = "original"
    elif fault == "second_attempt":
        record["attempt"] = 2
    elif fault == "processing_false":
        verification["processing_verified"] = False
    elif fault == "unknown":
        verification["unknown"] = ["Service Bus unavailable"]
    elif fault == "no_timestamps":
        verification.pop("baseline_at")
    elif fault == "same_timestamp":
        verification["baseline_at"] = verification["observed_at"]
    elif fault == "stale":
        now = datetime.now(timezone.utc)
        verification["baseline_at"] = (now - timedelta(hours=2)).isoformat()
        verification["observed_at"] = (now - timedelta(hours=1)).isoformat()
    elif fault == "same_count":
        verification["pipelines"][0]["progress"]["embedded_after"] = 0
    elif fault == "boolean_count":
        verification["pipelines"][0]["progress"]["embedded_after"] = True
    elif fault == "foreign_progress":
        verification["pipelines"][0]["pipeline_id"] = "foreign"
    elif fault == "no_final":
        events.pop(3)
    elif fault == "no_verification":
        events.pop(2)
    elif fault == "another_proposal":
        events.insert(4, {"type": "approval_required", "tool": "resume_pipeline"})
    elif fault == "missing_action_success":
        events[1]["result"].pop("success")
    elif fault == "nested_unknown":
        verification["pipelines"][0]["status"] = "UNKNOWN"
    elif fault == "boolean_attempt":
        record["attempt"] = True
    else:
        events[1]["result"]["error"] = "approved request failed"
    with pytest.raises((pod.Blocked, ValueError)):
        pod.recovery_proof(events, CALL, PID)


def test_model_prose_and_readonly_verification_cannot_substitute_approved_action():
    events = [
        {"type": "tool_result", "id": CALL, "name": "verify_recovery", "result": {"processing_verified": True}},
        {"type": "final", "text": "I repaired everything successfully."}, {"type": "done"},
    ]
    with pytest.raises(pod.Blocked):
        pod.recovery_proof(events, CALL, PID)


def chunk_rows():
    return [
        {"id": "chunk-" + str(i), "pipeline_id": PID, "source_id": SID,
         "source_ref": "owned-pending-document", "chunk_index": i, "chunk_count": 2,
         "text": text, "embedding": [0.1 + i / 10] * 1536}
        for i, text in enumerate(["first", "second"])
    ]


def test_actual_distinct_multichunk_embeddings_and_identity_required():
    result = pod.validate_chunks(chunk_rows(), PID, SID, ["first", "second"])
    assert result["chunks"] == 2 and result["duplicates"] == 0


@pytest.mark.parametrize("fault", [
    "missing_chunk", "duplicate_id", "foreign_source", "foreign_pipeline", "wrong_text",
    "bad_index", "zero_vector", "boolean_vector", "nonfinite_vector", "identical_vectors",
])
def test_actual_destination_data_faults_fail_even_after_claimed_agent_success(fault):
    rows = chunk_rows()
    if fault == "missing_chunk":
        rows.pop()
    elif fault == "duplicate_id":
        rows[1]["id"] = rows[0]["id"]
    elif fault == "foreign_source":
        rows[0]["source_id"] = "original"
    elif fault == "foreign_pipeline":
        rows[0]["pipeline_id"] = "original"
    elif fault == "wrong_text":
        rows[0]["text"] = "stale"
    elif fault == "bad_index":
        rows[0]["chunk_index"] = 9
    elif fault == "zero_vector":
        rows[0]["embedding"] = [0] * 1536
    elif fault == "boolean_vector":
        rows[0]["embedding"] = [True] * 1536
    elif fault == "nonfinite_vector":
        rows[0]["embedding"][0] = float("nan")
    else:
        rows[1]["embedding"] = rows[0]["embedding"]
    with pytest.raises(pod.Blocked):
        pod.validate_chunks(rows, PID, SID, ["first", "second"])


@pytest.fixture
def scenario(monkeypatch):
    value = pod.Scenario.__new__(pod.Scenario)
    value.prefix = PREFIX
    value.source_id, value.destination_id, value.pipeline_id = SID, "dst-owned", PID
    value.approval_at = None
    value.action_observed = False
    value.report = {"result": "blocked", "prefix": PREFIX, "repair_actor": "none"}
    value.deadline = pod.time.monotonic() + 1200
    value.http = Mock()
    monkeypatch.setattr(pod.time, "sleep", Mock())
    monkeypatch.setattr(pod, "arm_deadline", Mock())
    return value


@pytest.mark.parametrize("action", ["resume", "run", "reset", "delete"])
def test_harness_manual_pipeline_repair_is_prohibited(scenario, action):
    with pytest.raises(pod.Blocked, match="manual_action_not_allowlisted"):
        scenario.call("POST", "pipelines/" + PID + "/" + action)
    scenario.http.request.assert_not_called()


def test_api_requests_use_loopback_without_spoofed_internal_headers(scenario):
    scenario.http.request.return_value.status_code = 200
    scenario.http.request.return_value.json.return_value = {}
    scenario.call("GET", "agent/tools")
    call = scenario.http.request.call_args
    assert call.args == ("GET", "http://127.0.0.1:8080/api/agent/tools")
    assert "headers" not in call.kwargs


def test_ambiguous_approval_is_single_post_not_retried(scenario):
    scenario.stream = Mock(side_effect=[proposal_events(), TimeoutError("response lost private")])
    scenario.call = Mock(return_value=pending())
    scenario.owned_pipeline = Mock(return_value={"status": "paused"})
    scenario.rows = Mock(return_value=[])
    scenario.emit = Mock()
    with pytest.raises(TimeoutError):
        scenario.request_and_approve()
    assert [call.args[0] for call in scenario.stream.call_args_list] == ["agent/chat", "agent/chat/approve"]
    payload = scenario.stream.call_args.args[1]
    assert payload["decision"] == "approve" and payload["call_id"] == CALL and payload["session_id"] == SESSION
    assert scenario.report["approval_attempts"] == 1
    assert scenario.report["repair_actor"] == "none"


@pytest.mark.parametrize("failure_stage", ["create_fixture", "wait_for_cached_health", "request_and_approve", "verify_data"])
def test_finally_cleanup_runs_on_every_scenario_failure(scenario, failure_stage):
    for name in ("preflight", "create_fixture", "wait_for_cached_health", "request_and_approve", "verify_data"):
        setattr(scenario, name, Mock())
    getattr(scenario, failure_stage).side_effect = TimeoutError("private dependency details")
    scenario.cleanup = Mock()
    scenario.emit = Mock()
    report = scenario.run()
    scenario.cleanup.assert_called_once()
    assert report["result"] != "passed"
    assert "private" not in json.dumps(report)
    assert report["repair_actor"] == "none"


def test_final_cleanup_only_pauses_exact_new_pipeline(scenario):
    scenario.owned_pipeline = Mock(return_value={"status": "paused"})
    scenario.call = Mock()
    scenario.cleanup()
    scenario.call.assert_called_once_with("POST", "pipelines/" + PID + "/pause")
    assert scenario.report["cleanup"]["status"] == "paused"
    assert scenario.report["cleanup"]["manual_resume_executed"] is False


def test_cleanup_refuses_changed_ownership(scenario):
    scenario.owned_pipeline = Mock(side_effect=pod.Blocked("pipeline_ownership_lost"))
    scenario.call = Mock()
    with pytest.raises(pod.Blocked):
        scenario.cleanup()
    scenario.call.assert_not_called()


def test_cleanup_ambiguity_waits_before_final_pause_without_retrying_approval(scenario):
    scenario.approval_at = pod.time.monotonic()
    scenario.owned_pipeline = Mock(return_value={"status": "paused"})
    scenario.call = Mock()
    scenario.cleanup()
    assert scenario.report["approval_transport"].startswith("ambiguous_no_retry")
    assert pod.time.sleep.call_args_list[0].args[0] > 80
    assert [call.args[1] for call in scenario.call.call_args_list] == ["pipelines/" + PID + "/pause"]


def test_final_pause_failure_prevents_overall_pass(scenario):
    for name in ("preflight", "create_fixture", "wait_for_cached_health", "request_and_approve", "verify_data"):
        setattr(scenario, name, Mock())
    scenario.cleanup = Mock(side_effect=TimeoutError("pause unavailable"))
    scenario.emit = Mock()
    assert scenario.run()["result"] == "failed"
    assert scenario.report["cleanup"] == "unverified_owned_fixture_retained"


def test_health_wait_accepts_existing_api_naive_utc_timestamps(scenario):
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    health = {kind: [{"id": ident, "status": "healthy", "checked_at": stamp}] for kind, ident in (
        ("sources", SID), ("destinations", "dst-owned"), ("models", pod.EMBEDDING_MODEL),
    )}
    scenario.call = Mock(return_value=health)
    scenario.wait_for_cached_health()
    assert all(call.args == ("GET", "health/checks") for call in scenario.call.call_args_list)


def test_host_report_does_not_use_other_prefix_or_raw_error_output():
    output = "private raw exception\nAGENT_RECOVERY_REPORT=" + json.dumps({
        "prefix": "foreign", "result": "passed",
    })
    result = host.decode_report(output, PREFIX)
    assert result["result"] == "blocked" and "private" not in json.dumps(result)
