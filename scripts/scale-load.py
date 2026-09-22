"""Bounded HTTP scale runner for repeatable OmniVec load profiles.

Plan mode is the default and performs no network calls. Live execution requires
an exact confirmation phrase and writes a new structured report.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import http.client
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request


CONFIRMATION = "OMNIVEC-SCALE-LOAD-AUTHORIZED"
MAX_CONCURRENCY = 500
MAX_REQUESTS_PER_PHASE = 1_000_000
MAX_TOTAL_REQUESTS = 1_000_000
MAX_PHASES = 100
MAX_REQUESTS_PER_SECOND = 10_000
MAX_TIMEOUT_SECONDS = 300
MAX_RESPONSE_BYTES = 1_048_576
MAX_CONFIG_BYTES = 1_048_576
MAX_ESTIMATED_PHASE_SECONDS = 86_400
MAX_NAME_LENGTH = 128
MAX_URL_LENGTH = 2_048
MAX_HEADER_VALUE_LENGTH = 8_192
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
RESERVED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
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


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidConfiguration(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def reject_json_constant(value):
    raise InvalidConfiguration(f"Invalid JSON constant: {value}")


def load_config(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    require(len(raw) <= MAX_CONFIG_BYTES, "Configuration exceeds the size limit")
    try:
        config = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise InvalidConfiguration("Configuration must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise InvalidConfiguration("Configuration must contain valid JSON") from exc
    validate_config(config)
    return config


def validate_config(config):
    require(isinstance(config, dict), "Configuration must be an object")
    require(set(config) == CONFIG_FIELDS, "Unknown or missing configuration fields")
    require(
        isinstance(config["name"], str)
        and config["name"].strip()
        and len(config["name"]) <= MAX_NAME_LENGTH,
        "name must be a non-empty bounded string",
    )

    require(
        isinstance(config["target_url"], str)
        and len(config["target_url"]) <= MAX_URL_LENGTH
        and not any(character.isspace() for character in config["target_url"]),
        "target_url must be a bounded string without whitespace",
    )
    parsed = urlsplit(config["target_url"])
    require(parsed.scheme in ("http", "https") and parsed.hostname, "target_url must be HTTP(S)")
    try:
        parsed.port
    except ValueError as exc:
        raise InvalidConfiguration("target_url contains an invalid port") from exc
    require(not parsed.username and not parsed.password, "target_url must not contain credentials")
    require(not parsed.query and not parsed.fragment, "target_url must not contain query parameters or fragments")
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
        require(
            isinstance(header, str) and HEADER_NAME.fullmatch(header),
            "Header names must be valid HTTP field names",
        )
        require(header.lower() not in RESERVED_HEADERS, "Reserved transport headers are not allowed")
        require(isinstance(variable, str) and ENV_NAME.fullmatch(variable), "Invalid environment variable name")
    require(config["body"] is None or isinstance(config["body"], (dict, list)), "body must be JSON or null")
    require(config["method"] == "POST" or config["body"] is None, "GET profiles cannot define a body")
    if config["body"] is not None:
        try:
            json.dumps(config["body"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidConfiguration("body must contain finite JSON values") from exc

    timeout = config["request_timeout_seconds"]
    require(is_int(timeout) and 1 <= timeout <= MAX_TIMEOUT_SECONDS, "Invalid request timeout")
    phases = config["phases"]
    require(
        isinstance(phases, list) and 1 <= len(phases) <= MAX_PHASES,
        "Phase count exceeds the safety limit",
    )
    names = set()
    for phase in phases:
        require(isinstance(phase, dict) and set(phase) == PHASE_FIELDS, "Invalid phase fields")
        require(
            isinstance(phase["name"], str)
            and phase["name"].strip()
            and len(phase["name"]) <= MAX_NAME_LENGTH,
            "Phase name must be a non-empty bounded string",
        )
        require(phase["name"] not in names, "Phase names must be unique")
        names.add(phase["name"])
        require(
            is_int(phase["requests"]) and 1 <= phase["requests"] <= MAX_REQUESTS_PER_PHASE,
            "Invalid phase request count",
        )
        require(
            is_int(phase["concurrency"]) and 1 <= phase["concurrency"] <= MAX_CONCURRENCY,
            "Invalid phase concurrency",
        )
        require(
            is_int(phase["requests_per_second"])
            and 0 <= phase["requests_per_second"] <= MAX_REQUESTS_PER_SECOND,
            "Invalid phase request rate",
        )
        batches = math.ceil(phase["requests"] / phase["concurrency"])
        pacing = (
            (phase["requests"] - 1) / phase["requests_per_second"]
            if phase["requests_per_second"]
            else 0
        )
        require(
            batches * timeout + pacing <= MAX_ESTIMATED_PHASE_SECONDS,
            "Estimated phase duration exceeds the safety limit",
        )
    require(
        sum(phase["requests"] for phase in phases) <= MAX_TOTAL_REQUESTS,
        "Total requests exceed the safety limit",
    )

    thresholds = config["thresholds"]
    require(isinstance(thresholds, dict) and set(thresholds) == THRESHOLD_FIELDS, "Invalid thresholds")
    require(
        is_finite_number(thresholds["max_error_rate"])
        and 0 <= thresholds["max_error_rate"] <= 1,
        "max_error_rate must be between 0 and 1",
    )
    require(
        is_finite_number(thresholds["max_p95_ms"]) and thresholds["max_p95_ms"] > 0,
        "max_p95_ms must be positive",
    )
    require(
        is_int(thresholds["min_requests"]) and thresholds["min_requests"] > 0,
        "min_requests must be positive",
    )
    require(
        thresholds["min_requests"] <= sum(phase["requests"] for phase in phases),
        "min_requests cannot exceed configured requests",
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


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secure_urlopen(request, timeout):
    return urllib.request.build_opener(NoRedirectHandler()).open(request, timeout=timeout)


def request_once(request, timeout, opener=secure_urlopen, clock=time.perf_counter):
    started = clock()
    status = None
    error = None
    try:
        with opener(request, timeout=timeout) as response:
            status = int(response.status)
            if len(response.read(MAX_RESPONSE_BYTES + 1)) > MAX_RESPONSE_BYTES:
                error = "response_too_large"
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        error = "http_error"
    except urllib.error.URLError as exc:
        error = "timeout" if isinstance(exc.reason, TimeoutError) else "transport_error"
    except TimeoutError:
        error = "timeout"
    except http.client.HTTPException:
        error = "transport_error"
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
        require(
            len(value) <= MAX_HEADER_VALUE_LENGTH
            and all(ord(character) >= 32 and ord(character) != 127 for character in value),
            f"Invalid HTTP header value from environment variable: {variable}",
        )
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
        "failed_requests": failures,
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


def run_phase(config, phase, opener=None, clock=time.perf_counter, sleep=time.sleep):
    if opener is None:
        opener = urllib.request.build_opener(NoRedirectHandler()).open
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
    failures = sum(phase["failed_requests"] for phase in phases)
    latencies = [phase["latency_ms"]["p95"] for phase in phases if phase["latency_ms"]["p95"] is not None]
    checks = {
        "minimum_requests": completed >= config["thresholds"]["min_requests"],
        "maximum_error_rate": (failures / completed if completed else 1)
        <= config["thresholds"]["max_error_rate"],
        "maximum_phase_p95": bool(latencies)
        and max(latencies) <= config["thresholds"]["max_p95_ms"],
    }
    return checks, all(checks.values())


def execute(config, opener=None):
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


def write_report(path, result):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="x",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(result, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
    write_report(path, result)
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
