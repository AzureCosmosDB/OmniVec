"""OmniVec Telemetry — Azure Monitor / Application Insights integration.

Usage:
    from telemetry import init_telemetry, track_metric, track_event

    init_telemetry()  # call once at startup
    track_metric("omnivec.documents.embedded", 1, {"pipeline_id": "pip-123"})
    track_event("pipeline_created", {"pipeline_id": "pip-123", "source_type": "cosmosdb"})
"""

import os
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Global telemetry state
_initialized = False
_meter = None
_tracer = None
_counters = {}
_histograms = {}


def init_telemetry():
    """Initialize Azure Monitor OpenTelemetry if connection string is available."""
    global _initialized, _meter, _tracer

    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if not conn_str:
        logger.info("No APPLICATIONINSIGHTS_CONNECTION_STRING — telemetry disabled.")
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

        # Pre-create common metrics
        _counters["documents_embedded"] = _meter.create_counter(
            "omnivec.documents.embedded",
            description="Number of documents successfully embedded",
            unit="documents",
        )
        _counters["jobs_created"] = _meter.create_counter(
            "omnivec.pipeline.jobs_created",
            description="Number of jobs created by the controller",
            unit="jobs",
        )
        _counters["jobs_failed"] = _meter.create_counter(
            "omnivec.pipeline.jobs_failed",
            description="Number of jobs that failed processing",
            unit="jobs",
        )
        _counters["search_queries"] = _meter.create_counter(
            "omnivec.search.queries",
            description="Number of vector search queries",
            unit="queries",
        )
        _counters["api_errors"] = _meter.create_counter(
            "omnivec.api.errors",
            description="Number of API errors",
            unit="errors",
        )
        _histograms["embedding_latency"] = _meter.create_histogram(
            "omnivec.embedding.latency",
            description="Embedding generation latency",
            unit="ms",
        )
        _histograms["search_latency"] = _meter.create_histogram(
            "omnivec.search.latency",
            description="Vector search latency",
            unit="ms",
        )

        _initialized = True
        logger.info("Azure Monitor telemetry initialized.")
    except ImportError:
        logger.warning("azure-monitor-opentelemetry not installed — telemetry disabled.")
    except Exception as e:
        logger.warning(f"Failed to initialize telemetry: {e}")


def track_metric(name: str, value: float = 1, attributes: Optional[Dict] = None):
    """Increment a counter metric."""
    if not _initialized:
        return
    counter = _counters.get(name)
    if counter:
        counter.add(value, attributes or {})


def track_histogram(name: str, value: float, attributes: Optional[Dict] = None):
    """Record a histogram value (latency, size, etc.)."""
    if not _initialized:
        return
    hist = _histograms.get(name)
    if hist:
        hist.record(value, attributes or {})


def track_event(name: str, properties: Optional[Dict] = None):
    """Track a custom event via trace span."""
    if not _initialized or not _tracer:
        return
    with _tracer.start_as_current_span(name) as span:
        if properties:
            for k, v in properties.items():
                span.set_attribute(k, str(v))


class Timer:
    """Context manager for timing operations and recording to histogram."""

    def __init__(self, metric_name: str, attributes: Optional[Dict] = None):
        self.metric_name = metric_name
        self.attributes = attributes or {}
        self.start = None

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        elapsed_ms = (time.time() - self.start) * 1000
        track_histogram(self.metric_name, elapsed_ms, self.attributes)
