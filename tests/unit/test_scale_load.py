import importlib.util
import io
import json
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
    ],
)
def test_configuration_fails_closed(mutation, message):
    value = config()
    mutation(value)
    with pytest.raises(scale.InvalidConfiguration, match=message):
        scale.validate_config(value)


def test_secret_headers_are_loaded_but_not_added_to_plan(monkeypatch):
    value = config()
    value["headers_from_env"] = {"Authorization": "OMNIVEC_TEST_TOKEN"}
    monkeypatch.setenv("OMNIVEC_TEST_TOKEN", "Bearer private-value")
    request = scale.build_request(value)
    assert request.headers["Authorization"] == "Bearer private-value"
    assert "private-value" not in json.dumps(scale.plan(value))


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
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
    assert result["statuses"] == {"200": 4}
    assert result["error_rate"] == 0
    assert result["latency_ms"]["p95"] >= 0


@pytest.mark.parametrize(
    "exception,category,status",
    [
        (urllib.error.URLError("offline"), "transport_error", None),
        (TimeoutError(), "timeout", None),
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


def test_thresholds_fail_on_error_rate_or_latency():
    value = config()
    phases = [{
        "completed_requests": 4,
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
