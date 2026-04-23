#!/usr/bin/env python3
"""Progress Tracker - Real-time progress and status tracking.

Tracks:
- Per-source progress (backfill %, live lag, pending jobs)
- Per-pipeline progress (aggregated across sources)
- System-wide progress

All progress stored in CosmosDB for durability and querying.
"""

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from enum import Enum

from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from store import get_store

logger = logging.getLogger(__name__)


class SourceStatus(str, Enum):
    """Source processing status."""
    PENDING = "pending"           # Not started
    STARTING = "starting"         # Workers being created
    BACKFILLING = "backfilling"   # Historical data processing
    LIVE = "live"                 # Processing live changes only
    PAUSED = "paused"             # Manually paused
    ERROR = "error"               # Processing error
    COMPLETED = "completed"       # Backfill complete, live caught up


class PipelineStatus(str, Enum):
    """Pipeline processing status."""
    ACTIVE = "active"             # Processing normally
    PAUSED = "paused"             # Manually paused
    ERROR = "error"               # Has errors
    DEGRADED = "degraded"         # Some sources failing


class ProgressTracker:
    """Tracks and reports processing progress with atomic updates."""

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.progress_id = f"progress-{source_id}"
        self._current_etag: Optional[str] = None

    def get_progress(self) -> Optional[Dict[str, Any]]:
        """Get current progress for source.

        Returns None only when the document does not exist. Transient Cosmos
        errors (429/5xx) are retried by the store decorator and, if still
        failing, propagate so callers can decide whether to proceed or back off
        — previously they were silently swallowed and looked identical to
        'not found'.
        """
        store = get_store()
        doc = store.get(self.progress_id, partition_key="progress")
        if doc is None:
            self._current_etag = None
            return None
        self._current_etag = doc.get("_etag")
        return {k: v for k, v in doc.items() if not k.startswith("_")}

    def _save_with_etag(self, doc: Dict[str, Any], max_retries: int = 5) -> bool:
        """Save document with etag-based optimistic concurrency.

        On etag conflicts, reloads the latest doc and retries with jittered
        backoff. Returns False if we exhaust retries against a contended
        document; raises on non-retryable errors (the underlying store decorator
        already retries transient 429/5xx at a lower layer).
        """
        store = get_store()

        for attempt in range(1, max_retries + 1):
            try:
                if self._current_etag:
                    store.replace_with_etag(doc, self._current_etag)
                else:
                    # Try create first to avoid race condition.
                    try:
                        store.create(doc)
                    except CosmosResourceExistsError:
                        # Another process created it — reload and retry with etag.
                        logger.debug(
                            "Progress doc %s created concurrently, reloading",
                            self.progress_id,
                        )
                        self.get_progress()
                        continue

                # Refresh etag after successful save.
                saved_doc = store.get(self.progress_id, partition_key="progress")
                if saved_doc is not None:
                    self._current_etag = saved_doc.get("_etag")
                return True

            except CosmosAccessConditionFailedError:
                # Concurrent update — reload, jittered backoff, retry.
                delay = min(0.05 * (2 ** (attempt - 1)), 1.0)
                delay += random.uniform(0, delay)
                logger.debug(
                    "Progress etag conflict for %s (attempt %d/%d); retrying in %.2fs",
                    self.progress_id, attempt, max_retries, delay,
                )
                time.sleep(delay)
                self.get_progress()  # reload to get new etag
                continue

        logger.warning(
            "Failed to save progress for %s after %d etag retries",
            self.progress_id, max_retries,
        )
        return False

    def _load_or_new(self, default_status: "SourceStatus", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Load progress doc or return a freshly initialized one.

        Consolidates the try/except-NotFound boilerplate previously duplicated
        across update_backfill_progress / update_live_progress / set_status.
        Transient errors now propagate (they used to be silently replaced with
        a blank default doc, which would overwrite real progress on the next
        save).
        """
        store = get_store()
        doc = store.get(self.progress_id, partition_key="progress")
        if doc is not None:
            self._current_etag = doc.get("_etag")
            return doc

        self._current_etag = None
        base = {
            "id": self.progress_id,
            "doc_type": "progress",
            "source_id": self.source_id,
            "status": default_status.value,
            "backfill": {"locations": {}, "totals": {}},
            "live": {},
            "workers": {},
            "health": {"status": "healthy", "issues": []},
            "created_at": datetime.utcnow().isoformat(),
        }
        if extra:
            base.update(extra)
        return base

    def update_backfill_progress(
        self,
        location: str,
        blobs_enumerated: int,
        jobs_created: int,
        jobs_completed: int,
        jobs_failed: int,
        jobs_pending: int,
        total_estimated: Optional[int] = None,
        continuation_token: Optional[str] = None,
        last_blob: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Update backfill progress for a location."""
        doc = self._load_or_new(SourceStatus.BACKFILLING)

        # Update location progress
        if "backfill" not in doc:
            doc["backfill"] = {"locations": {}, "totals": {}}
        if "locations" not in doc["backfill"]:
            doc["backfill"]["locations"] = {}

        percent = 0
        if total_estimated and total_estimated > 0:
            percent = round((blobs_enumerated / total_estimated) * 100, 1)

        # Estimate completion time
        eta = None
        if jobs_pending > 0 and jobs_completed > 0:
            # Calculate rate from recent progress
            rate_per_hour = jobs_completed / max(1, (datetime.utcnow() - datetime.fromisoformat(doc.get("created_at", datetime.utcnow().isoformat()))).total_seconds() / 3600)
            if rate_per_hour > 0:
                hours_remaining = jobs_pending / rate_per_hour
                eta = (datetime.utcnow() + timedelta(hours=hours_remaining)).isoformat()

        doc["backfill"]["locations"][location] = {
            "blobs_enumerated": blobs_enumerated,
            "jobs_created": jobs_created,
            "jobs_completed": jobs_completed,
            "jobs_failed": jobs_failed,
            "jobs_pending": jobs_pending,
            "total_estimated": total_estimated,
            "percent_complete": percent,
            "continuation_token": bool(continuation_token),
            "last_blob": last_blob,
            "estimated_completion": eta,
            "error": error,
            "updated_at": datetime.utcnow().isoformat(),
        }

        # Recalculate totals
        totals = {
            "blobs_enumerated": 0,
            "jobs_created": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "jobs_pending": 0,
            "total_estimated": 0,
        }
        for loc_data in doc["backfill"]["locations"].values():
            for key in totals:
                totals[key] += loc_data.get(key, 0) or 0

        if totals["total_estimated"] > 0:
            totals["percent_complete"] = round(
                (totals["blobs_enumerated"] / totals["total_estimated"]) * 100, 1
            )
        else:
            totals["percent_complete"] = 0

        doc["backfill"]["totals"] = totals

        # Update status
        if error:
            doc["status"] = SourceStatus.ERROR.value
            doc["status_reason"] = error
        elif totals["percent_complete"] >= 100 and totals["jobs_pending"] == 0:
            doc["status"] = SourceStatus.LIVE.value
            doc["status_reason"] = "Backfill complete"
        else:
            doc["status"] = SourceStatus.BACKFILLING.value
            doc["status_reason"] = f"Backfill {totals['percent_complete']}% complete"

        doc["updated_at"] = datetime.utcnow().isoformat()
        self._save_with_etag(doc)

    def update_live_progress(
        self,
        events_received: int,
        events_processed: int,
        events_pending: int,
        throughput_per_sec: float,
        error: Optional[str] = None,
    ):
        """Update live processing progress."""
        doc = self._load_or_new(
            SourceStatus.LIVE,
            extra={"backfill": {}, "live": {}},
        )

        # Calculate lag
        lag_seconds = 0
        if throughput_per_sec > 0 and events_pending > 0:
            lag_seconds = round(events_pending / throughput_per_sec, 1)

        doc["live"] = {
            "status": "error" if error else "running",
            "events_received": events_received,
            "events_processed": events_processed,
            "events_pending": events_pending,
            "lag_seconds": lag_seconds,
            "throughput_per_sec": throughput_per_sec,
            "error": error,
            "updated_at": datetime.utcnow().isoformat(),
        }

        if error:
            doc["status"] = SourceStatus.ERROR.value
            doc["status_reason"] = error
        else:
            doc["status"] = SourceStatus.LIVE.value
            doc["status_reason"] = f"Live - lag {lag_seconds}s"

        doc["updated_at"] = datetime.utcnow().isoformat()
        self._save_with_etag(doc)

    def update_workers(
        self,
        worker_type: str,
        desired: int,
        ready: int,
        processing: int,
    ):
        """Update worker status. No-op if progress doc does not exist yet."""
        store = get_store()
        doc = store.get(self.progress_id, partition_key="progress")
        if doc is None:
            return
        self._current_etag = doc.get("_etag")

        if "workers" not in doc:
            doc["workers"] = {}

        doc["workers"][worker_type] = {
            "desired": desired,
            "ready": ready,
            "processing": processing,
            "updated_at": datetime.utcnow().isoformat(),
        }

        doc["updated_at"] = datetime.utcnow().isoformat()
        self._save_with_etag(doc)

    def set_status(self, status: SourceStatus, reason: str):
        """Set source status with reason."""
        doc = self._load_or_new(
            status,
            extra={"backfill": {}, "live": {}},
        )

        doc["status"] = status.value
        doc["status_reason"] = reason
        doc["updated_at"] = datetime.utcnow().isoformat()
        self._save_with_etag(doc)


def get_pipeline_progress(pipeline_id: str) -> Dict[str, Any]:
    """Get aggregated progress for a pipeline across all sources.

    Returns an error dict on caller-visible failures (pipeline missing,
    Cosmos unavailable after retries). Per-source lookup failures are
    surfaced as a per-source `status: unknown` entry with the specific
    error so operators can distinguish 'no progress yet' from 'Cosmos
    failing'.
    """
    store = get_store()

    # Get pipeline
    pipeline = store.get(pipeline_id, partition_key="pipeline")
    if pipeline is None:
        return {"error": "Pipeline not found"}

    # Get all source progress
    source_ids = pipeline.get("source_ids", [])
    sources_progress = {}
    totals = {
        "documents_indexed": 0,
        "documents_pending": 0,
        "documents_failed": 0,
        "sources_active": 0,
        "sources_error": 0,
        "sources_paused": 0,
    }

    for source_id in source_ids:
        try:
            progress = store.get(f"progress-{source_id}", partition_key="progress")
        except CosmosHttpResponseError as exc:
            logger.warning(
                "Cosmos error fetching progress for source %s: status=%s %s",
                source_id, getattr(exc, "status_code", None), exc,
            )
            sources_progress[source_id] = {
                "status": "unknown",
                "status_reason": f"Cosmos error ({getattr(exc, 'status_code', 'n/a')})",
            }
            continue

        if progress is None:
            sources_progress[source_id] = {
                "status": "unknown",
                "status_reason": "Progress not available",
            }
            continue

        sources_progress[source_id] = {
            "status": progress.get("status", "unknown"),
            "status_reason": progress.get("status_reason", ""),
            "backfill_percent": progress.get("backfill", {}).get("totals", {}).get("percent_complete", 0),
            "live_lag_seconds": progress.get("live", {}).get("lag_seconds", 0),
            "jobs_pending": progress.get("backfill", {}).get("totals", {}).get("jobs_pending", 0),
            "jobs_completed": progress.get("backfill", {}).get("totals", {}).get("jobs_completed", 0),
        }

        # Aggregate totals
        totals["documents_indexed"] += sources_progress[source_id]["jobs_completed"]
        totals["documents_pending"] += sources_progress[source_id]["jobs_pending"]

        status = progress.get("status", "")
        if status == SourceStatus.ERROR.value:
            totals["sources_error"] += 1
        elif status == SourceStatus.PAUSED.value:
            totals["sources_paused"] += 1
        else:
            totals["sources_active"] += 1

    # Determine pipeline status
    if totals["sources_error"] > 0:
        status = PipelineStatus.ERROR.value
        status_reason = f"{totals['sources_error']} source(s) have errors"
    elif totals["sources_paused"] == len(source_ids):
        status = PipelineStatus.PAUSED.value
        status_reason = "All sources paused"
    elif totals["sources_paused"] > 0:
        status = PipelineStatus.DEGRADED.value
        status_reason = f"{totals['sources_paused']} source(s) paused"
    else:
        status = PipelineStatus.ACTIVE.value
        status_reason = "Processing normally"

    # Calculate overall percent
    total_docs = totals["documents_indexed"] + totals["documents_pending"]
    overall_percent = 0
    if total_docs > 0:
        overall_percent = round((totals["documents_indexed"] / total_docs) * 100, 1)

    return {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline.get("name", ""),
        "status": status,
        "status_reason": status_reason,
        "sources": sources_progress,
        "totals": {
            **totals,
            "overall_percent": overall_percent,
        },
        "updated_at": datetime.utcnow().isoformat(),
    }


def get_all_sources_progress() -> List[Dict[str, Any]]:
    """Get progress for all sources."""
    store = get_store()
    query = "SELECT * FROM c WHERE c.doc_type = 'progress'"
    docs = store.query(query, [], partition_key="progress")
    return [{k: v for k, v in doc.items() if not k.startswith("_")} for doc in docs]


def get_system_progress() -> Dict[str, Any]:
    """Get system-wide progress summary."""
    sources = get_all_sources_progress()

    totals = {
        "total_sources": len(sources),
        "sources_backfilling": 0,
        "sources_live": 0,
        "sources_paused": 0,
        "sources_error": 0,
        "total_documents_indexed": 0,
        "total_documents_pending": 0,
        "total_documents_failed": 0,
    }

    for src in sources:
        status = src.get("status", "")
        if status == SourceStatus.BACKFILLING.value:
            totals["sources_backfilling"] += 1
        elif status == SourceStatus.LIVE.value:
            totals["sources_live"] += 1
        elif status == SourceStatus.PAUSED.value:
            totals["sources_paused"] += 1
        elif status == SourceStatus.ERROR.value:
            totals["sources_error"] += 1

        backfill = src.get("backfill", {}).get("totals", {})
        totals["total_documents_indexed"] += backfill.get("jobs_completed", 0)
        totals["total_documents_pending"] += backfill.get("jobs_pending", 0)
        totals["total_documents_failed"] += backfill.get("jobs_failed", 0)

    # Overall percent
    total = totals["total_documents_indexed"] + totals["total_documents_pending"]
    totals["overall_percent"] = 0
    if total > 0:
        totals["overall_percent"] = round((totals["total_documents_indexed"] / total) * 100, 1)

    return {
        "status": "healthy" if totals["sources_error"] == 0 else "degraded",
        "totals": totals,
        "sources": sources,
        "updated_at": datetime.utcnow().isoformat(),
    }
