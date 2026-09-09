"""Dependency-failure/data-integrity suite; all Azure/HTTP objects are fakes."""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
import urllib.error

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("chaos_data_probes", SCRIPTS / "recovery_chaos_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def subject(monkeypatch):
    value = probe.Probe.__new__(probe.Probe)
    value.c = json.loads((SCRIPTS / "recovery-chaos-pr183.json").read_text())
    value.run_id = "a" * 32
    value.name = "pr183-chaos-" + value.run_id + ".txt"
    value.storage = Mock()
    value.credential = object()
    value.payload = {"baseline": {}}
    # Never wait on real time or touch a real cloud client.
    monkeypatch.setattr(probe.time, "sleep", Mock())
    return value


def originals(subject):
    return [{
        "id": "vector-" + ref, "source_ref": ref,
        "source_id": subject.c["source_id"], "content_hash": "original-" + ref,
        "embedding": [0.1] * 1536,
    } for ref in subject.c["expected_refs"]]


def with_fixture(subject):
    rows = originals(subject)
    subject.payload["baseline"]["snapshot"] = probe.snapshot(rows)
    return rows + [{
        "id": "vector-fixture", "source_ref": subject.name,
        "source_id": subject.c["source_id"], "content_hash": "owned",
        "embedding": [0.2] * 1536,
    }]


@pytest.mark.parametrize("vector", [
    None, [], [1] * 1535, [1] * 1537, [0] * 1536, ["1"] * 1536,
    [True] * 1536, [False] * 1536, [float("nan")] * 1536,
    [float("inf")] * 1536, [-float("inf")] * 1536,
])
def test_invalid_real_embedding_shapes_and_values_rejected(vector):
    assert not probe.valid_vector(vector)


def test_signed_finite_real_embedding_is_accepted():
    assert probe.valid_vector([-0.3, 0, 0.7] + [0.1] * 1533)


@pytest.mark.parametrize("mutation,message", [
    ("missing", "document set"), ("extra", "document set"),
    ("source", "Source identity"), ("id_collision", "identities"),
])
def test_scoped_documents_cannot_hide_loss_contamination_or_identity_change(subject, mutation, message):
    rows = originals(subject)
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append({**rows[0], "id": "unexpected", "source_ref": "unowned.txt"})
    elif mutation == "source":
        rows[0]["source_id"] = "different-source"
    else:
        rows[0]["id"] = rows[1]["id"]
    with pytest.raises(RuntimeError, match=message):
        probe.validate_rows(rows, subject.c)


@pytest.mark.parametrize("field,replacement", [
    ("id", "replaced-original"), ("content_hash", "modified-content"),
    ("embedding", [0.4] * 1536),
])
def test_recovery_rejects_original_identity_content_or_vector_drift(subject, field, replacement):
    rows = with_fixture(subject)
    rows[0][field] = replacement
    subject.rows = Mock(return_value=rows)
    with pytest.raises(RuntimeError, match="Original vector identity or content changed"):
        subject.verify()


def test_recovery_requires_two_real_persisted_observations(subject):
    subject.rows = Mock(return_value=with_fixture(subject))
    result = subject.verify()
    assert result == {
        "documents": 5, "originals_unchanged": True, "duplicates": 0,
        "owned_fixture_deleted": False,
    }
    assert subject.rows.call_count == 2


def test_delayed_duplicate_after_first_observation_fails(subject):
    rows = with_fixture(subject)
    subject.rows = Mock(side_effect=[rows, rows + [rows[-1]]])
    with pytest.raises(RuntimeError, match="Duplicate"):
        subject.verify()


def test_failed_processing_is_not_replaced_by_readiness_success(subject, monkeypatch):
    subject.payload["baseline"]["snapshot"] = probe.snapshot(originals(subject))
    subject.rows = Mock(return_value=originals(subject))

    def bounded_wait(check, timeout):
        assert check() is None
        raise RuntimeError("Probe deadline exceeded")

    monkeypatch.setattr(probe, "wait_until", bounded_wait)
    with pytest.raises(RuntimeError, match="deadline"):
        subject.verify()


def test_upload_never_overwrites_originals_and_marks_exact_run_ownership(subject):
    result = subject.upload()
    subject.storage.get_blob_client.assert_called_once_with(subject.c["blob_container"], subject.name)
    call = subject.storage.get_blob_client.return_value.upload_blob.call_args
    assert call.kwargs == {"overwrite": False, "metadata": {"pr183_chaos_run": subject.run_id}}
    assert subject.run_id.encode() in call.args[0]
    assert result == {"owned_blob": subject.name}
    assert subject.name not in subject.c["expected_refs"]


def test_ambiguous_upload_timeout_is_not_retried(subject):
    blob = subject.storage.get_blob_client.return_value
    blob.upload_blob.side_effect = TimeoutError("write response lost")
    with pytest.raises(TimeoutError):
        subject.upload()
    blob.upload_blob.assert_called_once()


@pytest.fixture
def blob_cleanup(subject, monkeypatch):
    core = ModuleType("azure.core")
    core.MatchConditions = SimpleNamespace(IfNotModified="IfNotModified")
    monkeypatch.setitem(sys.modules, "azure.core", core)
    blob = subject.storage.get_blob_client.return_value
    blob.exists.return_value = True
    blob.get_blob_properties.return_value = SimpleNamespace(
        metadata={"pr183_chaos_run": subject.run_id}, etag='"owned-etag"',
    )
    subject.verify = Mock(return_value={"owned_fixture_deleted": True, "documents": 4})
    return blob


def test_cleanup_deletes_only_metadata_and_etag_verified_owned_blob(subject, blob_cleanup):
    result = subject.cleanup()
    subject.storage.get_blob_client.assert_called_once_with(subject.c["blob_container"], subject.name)
    blob_cleanup.delete_blob.assert_called_once_with(
        etag='"owned-etag"', match_condition="IfNotModified",
    )
    subject.verify.assert_called_once_with(deleted=True)
    assert result["documents"] == 4


@pytest.mark.parametrize("metadata", [{}, {"pr183_chaos_run": "another-run"}])
def test_cleanup_refuses_missing_or_foreign_ownership(subject, blob_cleanup, metadata):
    blob_cleanup.get_blob_properties.return_value.metadata = metadata
    with pytest.raises(RuntimeError, match="ownership"):
        subject.cleanup()
    blob_cleanup.delete_blob.assert_not_called()
    subject.verify.assert_not_called()


def test_cleanup_does_not_retry_or_ignore_etag_race(subject, blob_cleanup):
    blob_cleanup.delete_blob.side_effect = RuntimeError("ETag conflict")
    with pytest.raises(RuntimeError, match="ETag"):
        subject.cleanup()
    blob_cleanup.delete_blob.assert_called_once()
    subject.verify.assert_not_called()


def test_already_deleted_blob_still_requires_vector_cleanup(subject, blob_cleanup):
    blob_cleanup.exists.return_value = False
    subject.cleanup()
    blob_cleanup.delete_blob.assert_not_called()
    subject.verify.assert_called_once_with(deleted=True)


def test_eventgrid_unavailable_cannot_report_successful_cleanup(subject, blob_cleanup):
    subject.verify.side_effect = TimeoutError("vector deletion never arrived")
    with pytest.raises(TimeoutError):
        subject.cleanup()
    blob_cleanup.delete_blob.assert_called_once()


def install_bus(monkeypatch, subject, messages):
    receiver = Mock()
    receiver.peek_messages.return_value = messages
    receiver_context = Mock()
    receiver_context.__enter__ = Mock(return_value=receiver)
    receiver_context.__exit__ = Mock(return_value=False)
    client = Mock()
    client.get_subscription_receiver.return_value = receiver_context
    client_context = Mock()
    client_context.__enter__ = Mock(return_value=client)
    client_context.__exit__ = Mock(return_value=False)
    module = ModuleType("azure.servicebus")
    module.ServiceBusClient = Mock(return_value=client_context)
    monkeypatch.setitem(sys.modules, "azure.servicebus", module)
    subject.rows = Mock(return_value=originals(subject))
    return receiver, client


def owned_message(subject, **updates):
    body = {
        "source_ref": subject.name, "pipeline_id": subject.c["pipeline_id"],
        "source_id": subject.c["source_id"], **updates,
    }
    return json.dumps(body)


def test_queue_observation_peeks_only_matching_owned_source_pipeline(subject, monkeypatch):
    receiver, client = install_bus(monkeypatch, subject, [
        "{malformed", "[]", "null", "123", owned_message(subject, source_id="other-source"),
        owned_message(subject, pipeline_id="other-pipeline"), owned_message(subject),
    ])
    result = subject.queued()
    assert result == {"owned_message_peeked": True, "processing_absent_during_outage": True}
    client.get_subscription_receiver.assert_called_once_with(
        topic_name=subject.c["topic"], subscription_name=subject.c["subscription_name"],
        socket_timeout=10,
    )
    assert [call[0] for call in receiver.method_calls] == ["peek_messages"]


@pytest.mark.parametrize("updates", [
    {"source_ref": "chaos-backlog.txt"}, {"source_id": "foreign"},
    {"pipeline_id": "foreign"},
])
def test_non_owned_queue_message_is_not_accepted_as_fault_evidence(subject, monkeypatch, updates):
    receiver, _ = install_bus(monkeypatch, subject, [owned_message(subject, **updates)])

    def bounded_wait(check, timeout):
        assert check() is None
        raise TimeoutError("no owned work observed")

    monkeypatch.setattr(probe, "wait_until", bounded_wait)
    with pytest.raises(TimeoutError):
        subject.queued()
    receiver.receive_messages.assert_not_called()
    receiver.complete_message.assert_not_called()
    receiver.dead_letter_message.assert_not_called()


def test_queued_message_with_already_processed_vector_is_not_outage_evidence(subject, monkeypatch):
    install_bus(monkeypatch, subject, [owned_message(subject)])
    subject.rows.return_value = with_fixture(subject)
    with pytest.raises(RuntimeError, match="processed before outage"):
        subject.queued()


def test_servicebus_unavailable_does_not_consume_or_purge(subject, monkeypatch):
    receiver, _ = install_bus(monkeypatch, subject, [])
    receiver.peek_messages.side_effect = TimeoutError("Service Bus unavailable")
    with pytest.raises(TimeoutError):
        subject.queued()
    assert [call[0] for call in receiver.method_calls] == ["peek_messages"]


def test_queue_scanning_is_bounded_without_consuming_foreign_backlog(subject, monkeypatch):
    class Message:
        def __init__(self, sequence):
            self.sequence_number = sequence

        def __str__(self):
            return "{}"

    receiver, _ = install_bus(monkeypatch, subject, [])
    receiver.peek_messages.side_effect = [
        [Message(page * 100 + number) for number in range(1, 101)] for page in range(10)
    ]

    def bounded_wait(check, timeout):
        assert check() is None
        raise TimeoutError("bounded backlog scan")

    monkeypatch.setattr(probe, "wait_until", bounded_wait)
    with pytest.raises(TimeoutError):
        subject.queued()
    assert receiver.peek_messages.call_count == 10
    assert receiver.peek_messages.call_args.kwargs["sequence_number"] == 901
    assert all(call[0] == "peek_messages" for call in receiver.method_calls)


def registry_responses(subject):
    c = subject.c
    return {
        "/api/pipelines/" + c["pipeline_id"]: {
            "destination_id": c["destination_id"], "sources": [{"source_id": c["source_id"]}],
            "docgrok_pipeline": c["route_ids"][0], "status": "active",
        },
        "/api/sources/" + c["source_id"]: {
            "type": "azure-blob", "enabled": True,
            "config": {"account_url": "https://" + c["blob_account"] + ".blob.core.windows.net",
                       "container": c["blob_container"]},
        },
        "/api/destinations/" + c["destination_id"]: {
            "type": "cosmosdb-vector", "enabled": True,
            "config": {"endpoint": "https://" + c["cosmos_account"] + ".documents.azure.com:443/",
                       "database": c["cosmos_database"], "container": c["cosmos_container"]},
        },
        "/api/models": {"models": [{"id": c["model_id"]}]},
        "/admin/models/registry": {"models": [{"id": c["model_id"]}]},
        "/admin/pipelines": {
            "pipelines": [{"id": route, "model_id": c["model_id"]} for route in c["route_ids"]],
        },
        "/embed/batch": {"outputs": [[[0.1] * 1536]]},
    }


@pytest.mark.parametrize("fault", [
    "destination_changed", "source_changed", "route_changed", "paused",
    "source_disabled", "source_account_changed", "destination_container_changed",
    "api_model_missing", "router_model_duplicated", "route_model_changed", "embedding_zero",
])
def test_registry_and_real_processing_contract_rejects_semantic_faults(subject, fault):
    c = subject.c
    responses = registry_responses(subject)
    pipeline = responses["/api/pipelines/" + c["pipeline_id"]]
    if fault == "destination_changed":
        pipeline["destination_id"] = "foreign"
    elif fault == "source_changed":
        pipeline["sources"][0]["source_id"] = "foreign"
    elif fault == "route_changed":
        pipeline["docgrok_pipeline"] = "foreign"
    elif fault == "paused":
        pipeline["status"] = "paused"
    elif fault == "source_disabled":
        responses["/api/sources/" + c["source_id"]]["enabled"] = False
    elif fault == "source_account_changed":
        responses["/api/sources/" + c["source_id"]]["config"]["account_url"] = "https://foreign.invalid"
    elif fault == "destination_container_changed":
        responses["/api/destinations/" + c["destination_id"]]["config"]["container"] = "foreign"
    elif fault == "api_model_missing":
        responses["/api/models"]["models"] = []
    elif fault == "router_model_duplicated":
        responses["/admin/models/registry"]["models"] *= 2
    elif fault == "route_model_changed":
        responses["/admin/pipelines"]["pipelines"][0]["model_id"] = "foreign"
    else:
        responses["/embed/batch"]["outputs"] = [[[0.0] * 1536]]
    subject.request = Mock(side_effect=lambda path, *args, **kwargs: responses[path])
    with pytest.raises(RuntimeError):
        subject.registry()


def test_registry_verification_makes_real_embedding_request(subject):
    responses = registry_responses(subject)
    subject.request = Mock(side_effect=lambda path, *args, **kwargs: responses[path])
    result = subject.registry()
    assert result["dimensions"] == 1536
    call = subject.request.call_args_list[-1]
    assert call.args[0] == "/embed/batch"
    assert call.args[1]["model_id"] == subject.c["model_id"]
    assert call.kwargs == {"router": True}


@pytest.mark.parametrize("results", [[], [{"source_ref": "wrong-document.txt"}]])
def test_empty_or_wrong_semantic_top_result_is_processing_failure(subject, results):
    subject.request = Mock(return_value={"results": results})
    with pytest.raises(RuntimeError, match="Search"):
        subject.search()


def test_admin_auth_is_only_pod_loopback_and_never_router(subject, monkeypatch):
    monkeypatch.setenv("OMNIVEC_ADMIN_TOKEN", "fixture-token-not-a-real-secret")
    requests = []
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"{}"
    monkeypatch.setattr(probe.urllib.request, "urlopen",
                        lambda request, **kwargs: requests.append(request) or response)
    subject.request("/api/models")
    subject.request("/admin/models/registry", router=True)
    assert requests[0].full_url == "http://127.0.0.1:8080/api/models"
    assert requests[0].get_header("Authorization") == "Bearer fixture-token-not-a-real-secret"
    assert requests[1].full_url == "http://docgrok/admin/models/registry"
    assert requests[1].get_header("Authorization") is None


@pytest.mark.parametrize("failure", [
    TimeoutError("credential=private"),
    urllib.error.HTTPError("http://private.invalid?token=private", 503, "private", {}, None),
])
def test_pod_probe_redacts_unavailable_dependency_details(monkeypatch, capsys, failure):
    monkeypatch.setattr(sys, "argv", ["probe", "registry", "e30="])
    monkeypatch.setattr(probe, "Probe", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as exit_code:
        probe.main()
    assert exit_code.value.code == 1
    output = capsys.readouterr().out
    assert output == "CHAOS_ERROR=" + type(failure).__name__ + "\n"
    assert "private" not in output
