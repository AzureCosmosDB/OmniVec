import importlib.util
import http.client
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scale-load.py"
SPEC = importlib.util.spec_from_file_location("scale_load", SCRIPT)
scale = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale)


def config():
    return {
        "name": "test",
        "target_url": "https://search.example.test/health",
        "allowed_hosts": ["search.example.test"],
        "method": "GET",
        "headers_from_env": {},
        "body": None,
        "request_timeout_seconds": 5,
        "phases": [
            {"name": "load", "requests": 4, "concurrency": 2, "requests_per_second": 0}
        ],
        "thresholds": {"max_error_rate": 0, "max_p95_ms": 100, "min_requests": 4},
    }


def test_default_plan_performs_no_external_calls(monkeypatch, tmp_path, capsys):
    value = config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(scale.urllib.request, "urlopen", Mock(side_effect=AssertionError("network call")))
    assert scale.main(["--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["external_calls"] == 0
    assert output["total_requests"] == 4


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda value: value.update(target_url="http://search.example.test"), "require HTTPS"),
        (lambda value: value.update(allowed_hosts=["other.example.test"]), "not allowlisted"),
        (lambda value: value["phases"][0].update(concurrency=501), "concurrency"),
        (lambda value: value["phases"][0].update(requests=0), "request count"),
        (lambda value: value.update(extra=True), "Unknown or missing"),
        (lambda value: value.update(target_url="https://search.example.test/health?token=secret"), "query parameters"),
        (lambda value: value["headers_from_env"].update(Host="OMNIVEC_TEST_HOST"), "Reserved transport"),
        (lambda value: value["phases"][0].update(requests=True), "request count"),
        (lambda value: value["thresholds"].update(max_p95_ms=math.inf), "max_p95_ms"),
        (lambda value: value["thresholds"].update(min_requests=5), "configured requests"),
        (
            lambda value: value["phases"][0].update(
                requests=scale.MAX_REQUESTS_PER_PHASE,
                concurrency=1,
            ),
            "duration exceeds",
        ),
    ],
)
def test_configuration_fails_closed(mutation, message):
    value = config()
    mutation(value)
    with pytest.raises(scale.InvalidConfiguration, match=message):
        scale.validate_config(value)


@pytest.mark.parametrize(
    "content,message",
    [
        ('{"name":"first","name":"second"}', "Duplicate JSON field"),
        ('{"value":NaN}', "Invalid JSON constant"),
        ('{"value":', "valid JSON"),
        (b"\xff", "UTF-8 JSON"),
    ],
)
def test_config_loader_rejects_ambiguous_or_invalid_json(tmp_path, content, message):
    path = tmp_path / "config.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    with pytest.raises(scale.InvalidConfiguration, match=message):
        scale.load_config(path)


def test_config_loader_rejects_oversized_input(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b" " * (scale.MAX_CONFIG_BYTES + 1))
    with pytest.raises(scale.InvalidConfiguration, match="size limit"):
        scale.load_config(path)


def test_total_profile_size_is_bounded():
    value = config()
    value["phases"] = [
        {
            "name": f"phase-{index}",
            "requests": 10_000,
            "concurrency": 500,
            "requests_per_second": 10_000,
        }
        for index in range(scale.MAX_PHASES)
    ]
    value["phases"][0]["requests"] += 1
    value["thresholds"]["min_requests"] = 1
    with pytest.raises(scale.InvalidConfiguration, match="Total requests"):
        scale.validate_config(value)


def test_secret_headers_are_loaded_but_not_added_to_plan(monkeypatch):
    value = config()
    value["headers_from_env"] = {"Authorization": "OMNIVEC_TEST_TOKEN"}
    monkeypatch.setenv("OMNIVEC_TEST_TOKEN", "Bearer private-value")
    request = scale.build_request(value)
    assert request.headers["Authorization"] == "Bearer private-value"
    assert "private-value" not in json.dumps(scale.plan(value))


def test_secret_header_values_reject_newlines(monkeypatch):
    value = config()
    value["headers_from_env"] = {"Authorization": "OMNIVEC_TEST_TOKEN"}
    monkeypatch.setenv("OMNIVEC_TEST_TOKEN", "secret\r\nInjected: value")
    with pytest.raises(scale.InvalidConfiguration, match="Invalid HTTP header value"):
        scale.build_request(value)


def test_nonfinite_json_body_is_rejected():
    value = config()
    value["method"] = "POST"
    value["body"] = {"value": math.nan}
    with pytest.raises(scale.InvalidConfiguration, match="finite JSON"):
        scale.validate_config(value)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _size=-1):
        return b"{}"


def test_bounded_phase_reports_latency_status_and_throughput():
    ticks = iter(i / 1000 for i in range(100))
    result = scale.run_phase(
        config(),
        config()["phases"][0],
        opener=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(ticks),
        sleep=Mock(),
    )
    assert result["completed_requests"] == 4
    assert result["failed_requests"] == 0
    assert result["statuses"] == {"200": 4}
    assert result["error_rate"] == 0
    assert result["latency_ms"]["p95"] >= 0


@pytest.mark.parametrize(
    "exception,category,status",
    [
        (urllib.error.URLError("offline"), "transport_error", None),
        (urllib.error.URLError(TimeoutError()), "timeout", None),
        (TimeoutError(), "timeout", None),
        (http.client.IncompleteRead(b"partial"), "transport_error", None),
        (urllib.error.HTTPError("https://x", 429, "slow", {}, io.BytesIO()), "http_error", 429),
    ],
)
def test_request_failures_are_classified_without_response_content(exception, category, status):
    ticks = iter([0.0, 0.01])

    def fail(*_args, **_kwargs):
        raise exception

    result = scale.request_once(
        SimpleNamespace(),
        1,
        opener=fail,
        clock=lambda: next(ticks),
    )
    assert result == {
        "status": status,
        "error": category,
        "latency_ms": 10.0,
        "succeeded": False,
    }


def test_redirects_are_not_followed_or_sent_to_another_host():
    request = scale.urllib.request.Request("https://search.example.test/health")
    assert scale.NoRedirectHandler().redirect_request(
        request,
        io.BytesIO(),
        302,
        "Found",
        {"Location": "https://attacker.example/private"},
        "https://attacker.example/private",
    ) is None
    result = scale.request_once(
        request,
        1,
        opener=Mock(side_effect=urllib.error.HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://attacker.example/private"},
            io.BytesIO(),
        )),
        clock=iter([0.0, 0.01]).__next__,
    )
    assert result["status"] == 302
    assert result["error"] == "http_error"


def test_response_body_read_is_bounded_and_oversize_response_fails():
    response = Response()
    response.read = Mock(return_value=b"x" * (scale.MAX_RESPONSE_BYTES + 1))
    result = scale.request_once(
        SimpleNamespace(),
        1,
        opener=lambda *_args, **_kwargs: response,
        clock=iter([0.0, 0.01]).__next__,
    )
    response.read.assert_called_once_with(scale.MAX_RESPONSE_BYTES + 1)
    assert result["error"] == "response_too_large"
    assert not result["succeeded"]


def test_thresholds_fail_on_error_rate_or_latency():
    value = config()
    phases = [{
        "completed_requests": 4,
        "failed_requests": 1,
        "error_rate": 0.25,
        "latency_ms": {"p95": 250},
    }]
    checks, passed = scale.evaluate_thresholds(value, phases)
    assert checks == {
        "minimum_requests": True,
        "maximum_error_rate": False,
        "maximum_phase_p95": False,
    }
    assert not passed


def test_thresholds_use_exact_failure_counts():
    value = config()
    value["thresholds"]["max_error_rate"] = 0.2
    phases = [
        {
            "completed_requests": 3,
            "failed_requests": 1,
            "error_rate": 1 / 3,
            "latency_ms": {"p95": 10},
        },
        {
            "completed_requests": 2,
            "failed_requests": 0,
            "error_rate": 0,
            "latency_ms": {"p95": 10},
        },
    ]
    checks, passed = scale.evaluate_thresholds(value, phases)
    assert checks["maximum_error_rate"]
    assert passed


def test_live_mode_requires_confirmation_and_new_local_report(monkeypatch, tmp_path):
    value = config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(scale.InvalidConfiguration, match="confirmation"):
        scale.main(["--config", str(path), "--execute", "--report", "result.json"])
    (tmp_path / "result.json").write_text("{}")
    with pytest.raises(scale.InvalidConfiguration, match="new JSON"):
        scale.main([
            "--config", str(path),
            "--execute",
            "--confirm", scale.CONFIRMATION,
            "--report", "result.json",
        ])


def test_report_creation_is_atomic(tmp_path):
    path = tmp_path / "result.json"
    scale.write_report(path, {"result": "passed"})
    with pytest.raises(FileExistsError):
        scale.write_report(path, {"result": "overwritten"})
    assert json.loads(path.read_text()) == {"result": "passed"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_report_serialization_leaves_no_file(tmp_path):
    path = tmp_path / "result.json"
    with pytest.raises(ValueError):
        scale.write_report(path, {"invalid": math.nan})
    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []
