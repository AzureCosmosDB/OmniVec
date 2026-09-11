"""Bounded HTTP scale runner for repeatable OmniVec load profiles.

Plan mode is the default and performs no network calls. Live execution requires
an exact confirmation phrase and writes a new structured report.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request


CONFIRMATION = "OMNIVEC-SCALE-LOAD-AUTHORIZED"
MAX_CONCURRENCY = 500
MAX_REQUESTS_PER_PHASE = 1_000_000
MAX_REQUESTS_PER_SECOND = 10_000
MAX_TIMEOUT_SECONDS = 300
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CONFIG_FIELDS = {
    "name",
    "target_url",
    "allowed_hosts",
    "method",
    "headers_from_env",
    "body",
    "request_timeout_seconds",
    "phases",
    "thresholds",
}
PHASE_FIELDS = {"name", "requests", "concurrency", "requests_per_second"}
THRESHOLD_FIELDS = {"max_error_rate", "max_p95_ms", "min_requests"}


class InvalidConfiguration(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidConfiguration(message)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(config)
    return config


def validate_config(config):
    require(isinstance(config, dict), "Configuration must be an object")
    require(set(config) == CONFIG_FIELDS, "Unknown or missing configuration fields")
    require(isinstance(config["name"], str) and config["name"].strip(), "name is required")

    parsed = urlsplit(config["target_url"])
    require(parsed.scheme in ("http", "https") and parsed.hostname, "target_url must be HTTP(S)")
    require(not parsed.username and not parsed.password, "target_url must not contain credentials")
    if parsed.scheme != "https":
        require(parsed.hostname in ("localhost", "127.0.0.1", "::1"), "Non-local targets require HTTPS")
    allowed_hosts = config["allowed_hosts"]
    require(
        isinstance(allowed_hosts, list)
        and allowed_hosts
        and all(isinstance(host, str) and host for host in allowed_hosts),
        "allowed_hosts must be a non-empty string list",
    )
    require(parsed.hostname in allowed_hosts, "target_url host is not allowlisted")

    require(config["method"] in ("GET", "POST"), "method must be GET or POST")
    require(isinstance(config["headers_from_env"], dict), "headers_from_env must be an object")
    for header, variable in config["headers_from_env"].items():
        require(isinstance(header, str) and header.strip(), "Header names must be non-empty")
        require(isinstance(variable, str) and ENV_NAME.fullmatch(variable), "Invalid environment variable name")
    require(config["body"] is None or isinstance(config["body"], (dict, list)), "body must be JSON or null")
    require(config["method"] == "POST" or config["body"] is None, "GET profiles cannot define a body")

    timeout = config["request_timeout_seconds"]
    require(isinstance(timeout, int) and 1 <= timeout <= MAX_TIMEOUT_SECONDS, "Invalid request timeout")
    phases = config["phases"]
    require(isinstance(phases, list) and phases, "At least one phase is required")
    names = set()
    for phase in phases:
        require(isinstance(phase, dict) and set(phase) == PHASE_FIELDS, "Invalid phase fields")
        require(isinstance(phase["name"], str) and phase["name"].strip(), "Phase name is required")
        require(phase["name"] not in names, "Phase names must be unique")
        names.add(phase["name"])
        require(
            isinstance(phase["requests"], int) and 1 <= phase["requests"] <= MAX_REQUESTS_PER_PHASE,
            "Invalid phase request count",
        )
        require(
            isinstance(phase["concurrency"], int) and 1 <= phase["concurrency"] <= MAX_CONCURRENCY,
            "Invalid phase concurrency",
        )
        require(
            isinstance(phase["requests_per_second"], int)
            and 0 <= phase["requests_per_second"] <= MAX_REQUESTS_PER_SECOND,
            "Invalid phase request rate",
        )

    thresholds = config["thresholds"]
    require(isinstance(thresholds, dict) and set(thresholds) == THRESHOLD_FIELDS, "Invalid thresholds")
    require(
        isinstance(thresholds["max_error_rate"], (int, float))
        and 0 <= thresholds["max_error_rate"] <= 1,
        "max_error_rate must be between 0 and 1",
    )
    require(
        isinstance(thresholds["max_p95_ms"], (int, float)) and thresholds["max_p95_ms"] > 0,
        "max_p95_ms must be positive",
    )
    require(
        isinstance(thresholds["min_requests"], int) and thresholds["min_requests"] > 0,
        "min_requests must be positive",
    )


def plan(config):
    return {
        "mode": "plan",
        "external_calls": 0,
        "name": config["name"],
        "target": config["target_url"],
        "method": config["method"],
        "total_requests": sum(phase["requests"] for phase in config["phases"]),
        "max_concurrency": max(phase["concurrency"] for phase in config["phases"]),
        "phases": config["phases"],
        "live_status": "requires_explicit_confirmation",
    }


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def request_once(request, timeout, opener=urllib.request.urlopen, clock=time.perf_counter):
    started = clock()
    status = None
    error = None
    try:
        with opener(request, timeout=timeout) as response:
            status = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        error = "http_error"
    except urllib.error.URLError:
        error = "transport_error"
    except TimeoutError:
        error = "timeout"
    except OSError:
        error = "transport_error"
    elapsed_ms = max(0.0, (clock() - started) * 1000)
    succeeded = error is None and status is not None and 200 <= status < 400
    return {"status": status, "error": error, "latency_ms": elapsed_ms, "succeeded": succeeded}


def build_request(config):
    headers = {"Accept": "application/json", "User-Agent": "omnivec-scale-load/1"}
    for header, variable in config["headers_from_env"].items():
        value = os.environ.get(variable)
        require(value, f"Required environment variable is not set: {variable}")
        headers[header] = value
    payload = None
    if config["body"] is not None:
        payload = json.dumps(config["body"], separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(
        config["target_url"],
        data=payload,
        headers=headers,
        method=config["method"],
    )


def summarize_phase(phase, results, elapsed_seconds):
    latencies = [result["latency_ms"] for result in results]
    statuses = {}
    errors = {}
    for result in results:
        if result["status"] is not None:
            key = str(result["status"])
            statuses[key] = statuses.get(key, 0) + 1
        if result["error"]:
            errors[result["error"]] = errors.get(result["error"], 0) + 1
    failures = sum(not result["succeeded"] for result in results)
    return {
        "name": phase["name"],
        "configured_requests": phase["requests"],
        "completed_requests": len(results),
        "concurrency": phase["concurrency"],
        "requests_per_second": phase["requests_per_second"],
        "elapsed_seconds": elapsed_seconds,
        "throughput_rps": len(results) / elapsed_seconds if elapsed_seconds else 0,
        "error_rate": failures / len(results) if results else 1,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
        },
        "statuses": statuses,
        "errors": errors,
    }


def run_phase(config, phase, opener=urllib.request.urlopen, clock=time.perf_counter, sleep=time.sleep):
    request = build_request(config)
    results = []
    started = clock()
    interval = 1 / phase["requests_per_second"] if phase["requests_per_second"] else 0
    next_submission = started
    in_flight = set()
    with ThreadPoolExecutor(max_workers=phase["concurrency"]) as executor:
        for _ in range(phase["requests"]):
            while len(in_flight) >= phase["concurrency"]:
                completed, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                results.extend(future.result() for future in completed)
            if interval:
                delay = next_submission - clock()
                if delay > 0:
                    sleep(delay)
                next_submission = max(next_submission + interval, clock())
            in_flight.add(
                executor.submit(
                    request_once,
                    request,
                    config["request_timeout_seconds"],
                    opener,
                    clock,
                )
            )
        if in_flight:
            completed, _ = wait(in_flight)
            results.extend(future.result() for future in completed)
    return summarize_phase(phase, results, max(0.0, clock() - started))


def evaluate_thresholds(config, phases):
    completed = sum(phase["completed_requests"] for phase in phases)
    failures = sum(round(phase["error_rate"] * phase["completed_requests"]) for phase in phases)
    latencies = [phase["latency_ms"]["p95"] for phase in phases if phase["latency_ms"]["p95"] is not None]
    checks = {
        "minimum_requests": completed >= config["thresholds"]["min_requests"],
        "maximum_error_rate": (failures / completed if completed else 1)
        <= config["thresholds"]["max_error_rate"],
        "maximum_phase_p95": bool(latencies)
        and max(latencies) <= config["thresholds"]["max_p95_ms"],
    }
    return checks, all(checks.values())


def execute(config, opener=urllib.request.urlopen):
    started = time.time()
    phases = [run_phase(config, phase, opener=opener) for phase in config["phases"]]
    checks, passed = evaluate_thresholds(config, phases)
    return {
        "schema_version": 1,
        "name": config["name"],
        "target": config["target_url"],
        "method": config["method"],
        "started_at_epoch": started,
        "finished_at_epoch": time.time(),
        "phases": phases,
        "thresholds": config["thresholds"],
        "checks": checks,
        "result": "passed" if passed else "failed",
    }


def report_path(value):
    path = Path(value)
    require(path.parent.resolve() == Path.cwd().resolve(), "Report must be in the current directory")
    require(not path.exists() and path.suffix == ".json", "Report must be a new JSON file")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("scale-load-example.json")))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if not args.execute:
        print(json.dumps(plan(config), indent=2))
        return 0
    require(args.confirm == CONFIRMATION, "Exact live confirmation is required")
    require(args.report, "--report is required for live execution")
    path = report_path(args.report)
    result = execute(config)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
