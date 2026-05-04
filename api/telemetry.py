"""OmniVec Telemetry — Unified metrics system.

Architecture:
  - In-memory MetricsStore: thread-safe counters + sliding-window histograms.
    Always active. Powers /api/metrics/live for real-time dashboard.
  - Azure Monitor / App Insights: persistent, multi-replica-safe.
    Receives the same events via OpenTelemetry. Powers historical queries.
  - No CosmosDB metrics docs — zero DB cost for metrics reads.

Usage:
    from telemetry import (
        init_telemetry, metrics_store,
        record_embedding_batch, record_search, record_error,
        track_metric, track_histogram, track_event, Timer,
    )
    init_telemetry()
    record_embedding_batch(pipeline_id="p1", docs_embedded=50, docs_skipped=3,
                           tokens_used=12000, latency_ms=340)
    record_search(latency_ms=120, tokens_used=800, results_count=5)
"""

import os
import logging
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional  # lgtm[py/unused-import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory MetricsStore — always active, thread-safe
# ---------------------------------------------------------------------------

class _SlidingWindow:
    """Fixed-duration sliding window for latency/throughput samples."""

    def __init__(self, window_seconds: int = 300):
        self.window = window_seconds
        self._samples: deque = deque()  # (timestamp, value)
        self._lock = threading.Lock()

    def record(self, value: float):
        now = time.time()
        with self._lock:
            self._samples.append((now, value))
            self._trim(now)

    def _trim(self, now: float):
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)
            if not self._samples:
                return {"count": 0, "avg": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None}
            vals = sorted(v for _, v in self._samples)
            n = len(vals)
            return {
                "count": n,
                "avg": round(sum(vals) / n, 1),
                "p50": round(vals[n // 2], 1),
                "p95": round(vals[int(n * 0.95)], 1) if n >= 2 else round(vals[-1], 1),
                "p99": round(vals[int(n * 0.99)], 1) if n >= 2 else round(vals[-1], 1),
                "min": round(vals[0], 1),
                "max": round(vals[-1], 1),
            }


class _ThroughputTracker:
    """Rolling window throughput (events/sec)."""

    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        self._events: deque = deque()  # (timestamp, count)
        self._lock = threading.Lock()

    def record(self, count: int = 1):
        now = time.time()
        with self._lock:
            self._events.append((now, count))
            self._trim(now)

    def _trim(self, now: float):
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def rate(self) -> float:
        now = time.time()
        with self._lock:
            self._trim(now)
            if not self._events:
                return 0.0
            total = sum(c for _, c in self._events)
            span = now - self._events[0][0] if len(self._events) > 1 else self.window
            return round(total / max(span, 1), 2)


class MetricsStore:
    """Thread-safe in-memory metrics accumulator.

    Counters are global + per-pipeline. Histograms use sliding windows.
    This is the primary read source for the dashboard.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()

        # --- Global counters ---
        self.documents_embedded = 0
        self.documents_failed = 0
        self.documents_skipped_no_content = 0
        self.documents_skipped_unchanged = 0
        self.jobs_created = 0
        self.search_queries = 0
        self.api_errors_4xx = 0
        self.api_errors_5xx = 0
        self.tokens_embedding = 0    # tokens used for pipeline embedding
        self.tokens_search = 0       # tokens used for search-time embedding
        self.changefeed_batches = 0

        # --- Per-pipeline counters ---
        # { pipeline_id: {embedded, failed, skipped, tokens, ...} }
        self._pipelines: Dict[str, dict] = defaultdict(lambda: {
            "embedded": 0, "failed": 0, "skipped_no_content": 0,
            "skipped_unchanged": 0, "jobs_created": 0, "tokens": 0,
        })

        # --- Failure breakdown { error_category: count } ---
        self._failure_types: Dict[str, int] = defaultdict(int)

        # --- Sliding-window histograms (last 5 min) ---
        self.embedding_latency = _SlidingWindow(300)
        self.search_latency = _SlidingWindow(300)
        self.request_latency = _SlidingWindow(300)

        # --- Throughput tracker (rolling 60s) ---
        self.throughput = _ThroughputTracker(60)

        # --- Time-series buckets (minute granularity, last 24h) ---
        # { "2026-04-16T14:30:00": {processed, failed, processing_time_ms} }
        self._timeseries: Dict[str, dict] = defaultdict(lambda: {
            "processed": 0, "failed": 0, "processing_time_ms": 0.0,
        })

    # -- Recording methods (thread-safe) --

    def record_embedding_batch(self, pipeline_id: str, docs_embedded: int = 0,
                                docs_failed: int = 0, docs_skipped_no_content: int = 0,
                                docs_skipped_unchanged: int = 0, jobs_created: int = 0,
                                tokens_used: int = 0, latency_ms: float = 0,
                                source_id: str = ""):
        with self._lock:
            self.documents_embedded += docs_embedded
            self.documents_failed += docs_failed
            self.documents_skipped_no_content += docs_skipped_no_content
            self.documents_skipped_unchanged += docs_skipped_unchanged
            self.jobs_created += jobs_created
            self.tokens_embedding += tokens_used
            self.changefeed_batches += 1

            if pipeline_id:
                p = self._pipelines[pipeline_id]
                p["embedded"] += docs_embedded
                p["failed"] += docs_failed
                p["skipped_no_content"] += docs_skipped_no_content
                p["skipped_unchanged"] += docs_skipped_unchanged
                p["jobs_created"] += jobs_created
                p["tokens"] += tokens_used

            # Time-series bucket (minute granularity)
            bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00")
            ts = self._timeseries[bucket]
            ts["processed"] += docs_embedded
            ts["failed"] += docs_failed
            ts["processing_time_ms"] += latency_ms

        if docs_embedded > 0:
            self.throughput.record(docs_embedded)
        if latency_ms > 0:
            self.embedding_latency.record(latency_ms)

    def record_search(self, latency_ms: float = 0, tokens_used: int = 0,
                      results_count: int = 0, embed_latency_ms: float = 0):
        with self._lock:
            self.search_queries += 1
            self.tokens_search += tokens_used
        if latency_ms > 0:
            self.search_latency.record(latency_ms)
        if embed_latency_ms > 0:
            self.embedding_latency.record(embed_latency_ms)

    def record_error(self, status_code: int = 500, category: str = ""):
        with self._lock:
            if 400 <= status_code < 500:
                self.api_errors_4xx += 1
            else:
                self.api_errors_5xx += 1
            if category:
                self._failure_types[category] += 1

    def record_request(self, latency_ms: float):
        self.request_latency.record(latency_ms)

    def record_failure(self, pipeline_id: str = "", error_type: str = "unknown"):
        with self._lock:
            self.documents_failed += 1
            if pipeline_id:
                self._pipelines[pipeline_id]["failed"] += 1
            self._failure_types[error_type] += 1

    # -- Snapshot for dashboard --

    def snapshot(self) -> dict:
        """Return full metrics snapshot for /api/metrics/live."""
        with self._lock:
            pipelines = {}
            for pid, counters in self._pipelines.items():
                pipelines[pid] = dict(counters)

            total_tokens = self.tokens_embedding + self.tokens_search

            return {
                "started_at": self._started_at,
                "uptime_seconds": round(time.time() - datetime.fromisoformat(self._started_at).timestamp()),
                # Primary: throughput, latency, progress
                "throughput_docs_per_sec": self.throughput.rate(),
                "embedding_latency": self.embedding_latency.stats(),
                "search_latency": self.search_latency.stats(),
                "request_latency": self.request_latency.stats(),
                # Counters
                "documents_embedded": self.documents_embedded,
                "documents_failed": self.documents_failed,
                "jobs_created": self.jobs_created,
                "search_queries": self.search_queries,
                "changefeed_batches": self.changefeed_batches,
                # Secondary: tokens, skips, failure types
                "tokens": {
                    "embedding": self.tokens_embedding,
                    "search": self.tokens_search,
                    "total": total_tokens,
                },
                "skipped": {
                    "no_content": self.documents_skipped_no_content,
                    "unchanged": self.documents_skipped_unchanged,
                    "total": self.documents_skipped_no_content + self.documents_skipped_unchanged,
                },
                "errors": {
                    "client_4xx": self.api_errors_4xx,
                    "server_5xx": self.api_errors_5xx,
                    "failure_types": dict(self._failure_types),
                },
                # Per-pipeline breakdown
                "pipelines": pipelines,
            }

    def get_timeseries(self, start: str, end: str, granularity: str = "hour") -> list:
        """Return time-series buckets between start and end, aggregated by granularity."""
        gran_seconds = {"minute": 60, "hour": 3600, "day": 86400}.get(granularity, 3600)

        def _trunc(bucket_str: str) -> str:
            if granularity == "minute":
                return bucket_str  # already minute granularity
            elif granularity == "hour":
                return bucket_str[:13] + ":00:00"
            else:
                return bucket_str[:10] + "T00:00:00"

        with self._lock:
            agg: Dict[str, dict] = {}
            for bucket, data in self._timeseries.items():
                if bucket < start or bucket > end:
                    continue
                key = _trunc(bucket)
                if key not in agg:
                    agg[key] = {"processed": 0, "failed": 0, "processing_time_ms": 0.0}
                agg[key]["processed"] += data["processed"]
                agg[key]["failed"] += data["failed"]
                agg[key]["processing_time_ms"] += data["processing_time_ms"]

        buckets = []
        for t in sorted(agg.keys()):
            a = agg[t]
            p = a["processed"]
            f = a["failed"]
            buckets.append({
                "t": t,
                "processed": p,
                "failed": f,
                "throughput": round(p / gran_seconds, 1) if p > 0 else 0.0,
                "avg_latency_ms": round(a["processing_time_ms"] / p, 1) if p > 0 else None,
            })
        return buckets

    def reset(self):
        """Reset all counters (for testing or manual clear)."""
        with self._lock:
            for attr in ("documents_embedded", "documents_failed",
                         "documents_skipped_no_content", "documents_skipped_unchanged",
                         "jobs_created", "search_queries", "api_errors_4xx", "api_errors_5xx",
                         "tokens_embedding", "tokens_search", "changefeed_batches"):
                setattr(self, attr, 0)
            self._pipelines.clear()
            self._failure_types.clear()
            self._timeseries.clear()
            self._started_at = datetime.now(timezone.utc).isoformat()
        self.embedding_latency = _SlidingWindow(300)
        self.search_latency = _SlidingWindow(300)
        self.request_latency = _SlidingWindow(300)
        self.throughput = _ThroughputTracker(60)


# Singleton — importable anywhere
metrics_store = MetricsStore()


# ---------------------------------------------------------------------------
# Azure Monitor / App Insights — optional persistent layer
# ---------------------------------------------------------------------------

_initialized = False
_meter = None
_tracer = None
_counters: Dict = {}
_histograms: Dict = {}


def init_telemetry():
    """Initialize Azure Monitor if connection string is set.

    Always safe to call — no-ops if not configured.
    The in-memory MetricsStore works regardless.
    """
    global _initialized, _meter, _tracer

    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if not conn_str:
        logger.info("No APPLICATIONINSIGHTS_CONNECTION_STRING — App Insights disabled. "
                     "In-memory metrics still active.")
        return

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import metrics, trace

        configure_azure_monitor(
            connection_string=conn_str,
            enable_live_metrics=True,
        )

        _meter = metrics.get_meter("omnivec", "1.0.0")
        _tracer = trace.get_tracer("omnivec", "1.0.0")

        _counters["documents_embedded"] = _meter.create_counter(
            "omnivec.documents.embedded", unit="documents")
        _counters["documents_failed"] = _meter.create_counter(
            "omnivec.documents.failed", unit="documents")
        _counters["documents_skipped"] = _meter.create_counter(
            "omnivec.documents.skipped", unit="documents")
        _counters["jobs_created"] = _meter.create_counter(
            "omnivec.pipeline.jobs_created", unit="jobs")
        _counters["search_queries"] = _meter.create_counter(
            "omnivec.search.queries", unit="queries")
        _counters["tokens_used"] = _meter.create_counter(
            "omnivec.tokens.used", unit="tokens")
        _counters["api_errors"] = _meter.create_counter(
            "omnivec.api.errors", unit="errors")

        _histograms["embedding_latency"] = _meter.create_histogram(
            "omnivec.embedding.latency", unit="ms")
        _histograms["search_latency"] = _meter.create_histogram(
            "omnivec.search.latency", unit="ms")
        _histograms["request_latency"] = _meter.create_histogram(
            "omnivec.request.latency", unit="ms")

        _initialized = True
        logger.info("Azure Monitor telemetry initialized.")
    except ImportError:
        logger.warning("azure-monitor-opentelemetry not installed — App Insights disabled.")
    except Exception as e:
        logger.warning(f"Failed to init App Insights: {e}")


def _ai_counter(name: str, value: float, attrs: dict = None):
    """Forward to App Insights counter (no-op if not initialized)."""
    if _initialized:
        c = _counters.get(name)
        if c:
            c.add(value, attrs or {})


def _ai_histogram(name: str, value: float, attrs: dict = None):
    """Forward to App Insights histogram."""
    if _initialized:
        h = _histograms.get(name)
        if h:
            h.record(value, attrs or {})


# ---------------------------------------------------------------------------
# High-level recording functions — write to BOTH in-memory + App Insights
# ---------------------------------------------------------------------------

def record_embedding_batch(pipeline_id: str = "", docs_embedded: int = 0,
                           docs_failed: int = 0, docs_skipped_no_content: int = 0,
                           docs_skipped_unchanged: int = 0, jobs_created: int = 0,
                           tokens_used: int = 0, latency_ms: float = 0,
                           source_id: str = ""):
    """Record a changefeed embedding batch — primary metrics event."""
    attrs = {"pipeline_id": pipeline_id, "source_id": source_id}

    # In-memory
    metrics_store.record_embedding_batch(
        pipeline_id=pipeline_id, docs_embedded=docs_embedded,
        docs_failed=docs_failed, docs_skipped_no_content=docs_skipped_no_content,
        docs_skipped_unchanged=docs_skipped_unchanged, jobs_created=jobs_created,
        tokens_used=tokens_used, latency_ms=latency_ms, source_id=source_id,
    )

    # App Insights
    if docs_embedded:
        _ai_counter("documents_embedded", docs_embedded, attrs)
    if docs_failed:
        _ai_counter("documents_failed", docs_failed, attrs)
    if docs_skipped_no_content + docs_skipped_unchanged > 0:
        _ai_counter("documents_skipped", docs_skipped_no_content + docs_skipped_unchanged, attrs)
    if jobs_created:
        _ai_counter("jobs_created", jobs_created, attrs)
    if tokens_used:
        _ai_counter("tokens_used", tokens_used, attrs)
    if latency_ms > 0:
        _ai_histogram("embedding_latency", latency_ms, attrs)


def record_search(latency_ms: float = 0, embed_latency_ms: float = 0,
                  tokens_used: int = 0, results_count: int = 0):
    """Record a search query event."""
    metrics_store.record_search(
        latency_ms=latency_ms, tokens_used=tokens_used,
        results_count=results_count, embed_latency_ms=embed_latency_ms,
    )
    _ai_counter("search_queries", 1)
    if tokens_used:
        _ai_counter("tokens_used", tokens_used, {"source": "search"})
    if latency_ms > 0:
        _ai_histogram("search_latency", latency_ms)
    if embed_latency_ms > 0:
        _ai_histogram("embedding_latency", embed_latency_ms, {"source": "search"})


def record_error(status_code: int = 500, category: str = "", path: str = ""):
    """Record an API error."""
    metrics_store.record_error(status_code=status_code, category=category)
    _ai_counter("api_errors", 1, {"status_code": str(status_code), "category": category, "path": path})


def record_request(latency_ms: float, method: str = "", path: str = ""):
    """Record API request latency."""
    metrics_store.record_request(latency_ms)
    _ai_histogram("request_latency", latency_ms, {"method": method, "path": path})


def record_failure(pipeline_id: str = "", error_type: str = "unknown"):
    """Record a document/job failure with categorization."""
    metrics_store.record_failure(pipeline_id=pipeline_id, error_type=error_type)
    _ai_counter("documents_failed", 1, {"pipeline_id": pipeline_id, "error_type": error_type})


# ---------------------------------------------------------------------------
# Backward-compatible wrappers (for existing call sites)
# ---------------------------------------------------------------------------

def track_metric(name: str, value: float = 1, attributes: Optional[Dict] = None):
    """Legacy: increment a counter. Prefer record_* functions."""
    _ai_counter(name, value, attributes)


def track_histogram(name: str, value: float, attributes: Optional[Dict] = None):
    """Legacy: record a histogram value. Prefer record_* functions."""
    _ai_histogram(name, value, attributes)


def track_event(name: str, properties: Optional[Dict] = None):
    """Track a custom event via trace span."""
    if not _initialized or not _tracer:
        return
    with _tracer.start_as_current_span(name) as span:
        if properties:
            for k, v in properties.items():
                span.set_attribute(k, str(v))


class Timer:
    """Context manager for timing operations."""

    def __init__(self, metric_name: str, attributes: Optional[Dict] = None):
        self.metric_name = metric_name
        self.attributes = attributes or {}
        self.start = None
        self.elapsed_ms = 0

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = (time.time() - self.start) * 1000
        _ai_histogram(self.metric_name, self.elapsed_ms, self.attributes)
