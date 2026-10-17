"""Queries for deduplicated, asynchronously exported worker batch telemetry."""
import json
import threading
import time
from datetime import datetime, timezone


BATCHES_KQL = """let embeddingBatches = AppEvents
| where Name == 'omnivec.embedding.batch'
| extend pipeline_id = tostring(Properties.pipeline_id),
    batch_key = tostring(Properties.batch_key),
    model_id = tostring(Properties.model_id),
    reported_at = todatetime(Properties.reported_at)
| extend timestamp = coalesce(reported_at, TimeGenerated)
| where isnotempty(batch_key)
| summarize arg_max(TimeGenerated, *) by pipeline_id, batch_key
| project timestamp, pipeline_id, model_id, batch_key,
    processed = tolong(Properties.processed), failed = tolong(Properties.failed),
    total_time_ms = todouble(Properties.processing_time_ms),
    tokens = tolong(Properties.tokens_used), input_bytes = tolong(Properties.input_bytes),
    retries = tolong(Properties.retry_count), throttles = tolong(Properties.throttle_count),
    throttle_delay_ms = todouble(Properties.throttle_delay_ms),
    queue_wait_ms = todouble(Properties.queue_wait_ms),
    last_document = tostring(Properties.last_document);
"""

_cache = {}
_lock = threading.Lock()


def dimension_filter(name, value):
    # JSON escaping is also valid for Kusto quoted string literals.
    return f"| where {name} == {json.dumps(str(value))}\n" if value else ""


def cutoff_filter(cutoff):
    if not cutoff:
        return ""
    value = cutoff.isoformat() if hasattr(cutoff, "isoformat") else str(cutoff)
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
    return f"| where timestamp >= datetime({timestamp.astimezone(timezone.utc).isoformat()})\n"


def pipeline_summary(run_kql, pipeline_id=None, cutoff=None):
    from datetime import timedelta
    key = (pipeline_id, str(cutoff or ""))
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < 10:
            return cached[1]
    filters = dimension_filter("pipeline_id", pipeline_id) + cutoff_filter(cutoff)
    rows = run_kql(
        "embeddingBatches\n" + filters + """
| summarize processed=sum(processed), failed=sum(failed), total_time_ms=sum(total_time_ms),
    tokens=sum(tokens), input_bytes=sum(input_bytes), requests=count(), retries=sum(retries),
    throttles=sum(throttles), throttle_delay_ms=sum(throttle_delay_ms),
    queue_wait_ms=sum(queue_wait_ms), updated_at=max(timestamp),
    recent_processed=sumif(processed, timestamp >= ago(1m))
    , last_document=take_any(last_document)
    by pipeline_id
""", timedelta(days=7))
    result = None if rows is None else {
        str(row[0]): dict(zip(
            ("processed", "failed", "total_time_ms", "tokens", "input_bytes", "requests",
             "retries", "throttles", "throttle_delay_ms", "queue_wait_ms", "updated_at", "recent_processed", "last_document"),
            row[1:],
        )) for row in rows
    }
    with _lock:
        if len(_cache) >= 1000:
            _cache.clear()
        _cache[key] = (now, result)
    return result


def invalidate():
    with _lock:
        _cache.clear()
