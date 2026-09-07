#!/usr/bin/env python3
"""OmniVec Controller

Single-replica bookkeeper process:
- Health monitor: detects stuck PROCESSING jobs, retries or fails them
- DocGrok sync: restores pipelines and models from CosmosDB on startup
- Operational settings: applies K8s scaling changes

All source processing (change feed, blob enumeration, backfill) is handled
by the .NET changefeed service + .NET worker via Service Bus.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError

from models import Job, JobStatus, PipelineStatus, Pipeline
from store import init_store, get_store
from health_checker import run_health_checks, HEALTH_CHECK_INTERVAL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [controller] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

for _sdk_logger in ("azure.core.pipeline.policies.http_logging_policy",
                     "azure.identity", "azure.core", "urllib3"):
    logging.getLogger(_sdk_logger).setLevel(logging.WARNING)

# ── config ────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("CONTROLLER_POLL_INTERVAL", "10"))  # seconds
JOB_TIMEOUT_MINUTES = int(os.getenv("JOB_TIMEOUT_MINUTES", "10"))
MAX_RETRY_COUNT = int(os.getenv("MAX_RETRY_COUNT", "3"))
DOCGROK_URL = os.getenv("DOCGROK_URL", "http://docgrok:80")

_last_health_check: datetime | None = None


# ── helpers ───────────────────────────────────────────────────────────────

def _strip_doc(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


def _pipeline_from_doc(doc: dict) -> Pipeline:
    return Pipeline(**_strip_doc(doc))


def _job_from_doc(doc: dict) -> Job:
    return Job(**_strip_doc(doc))


def _to_doc(model, doc_type: str) -> dict:
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc


def _get_active_pipelines() -> list[Pipeline]:
    store = get_store()
    return [
        _pipeline_from_doc(d)
        for d in store.list("pipeline")
        if d.get("status") == PipelineStatus.ACTIVE.value
    ]




# ── job health monitor ───────────────────────────────────────────────────

def monitor_job_health():
    """Recover eligible jobs without overwriting concurrent worker updates."""
    store = get_store()
    now = datetime.now(timezone.utc)
    timeout_cutoff = now - timedelta(minutes=JOB_TIMEOUT_MINUTES)

    for doc in store.query(
        "SELECT * FROM c WHERE c.doc_type = 'job' AND c.status IN ('processing', 'failed')",
        partition_key="job",
    ):
        if doc.get("status") not in (JobStatus.PROCESSING.value, JobStatus.FAILED.value):
            continue
        try:
            job = _job_from_doc(doc)
            changed = False
            started_at = job.started_at or job.created_at
            if started_at is None and doc.get("_ts") is not None:
                started_at = datetime.fromtimestamp(doc["_ts"], tz=timezone.utc)
            if started_at and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if job.status == JobStatus.PROCESSING and started_at and started_at < timeout_cutoff:
                job.status = JobStatus.FAILED
                job.error = f"Timed out after {JOB_TIMEOUT_MINUTES} minutes"
                job.completed_at = now
                changed = True

            if job.status == JobStatus.FAILED and job.retry_count < MAX_RETRY_COUNT:
                pipeline = store.get(job.pipeline_id, "pipeline")
                # A paused/deleted pipeline must not consume retries or revive work.
                if pipeline and pipeline.get("status") == PipelineStatus.ACTIVE.value:
                    job.status = JobStatus.PENDING
                    job.error = None
                    job.retry_count += 1
                    job.started_at = None
                    job.completed_at = None
                    changed = True

            if changed:
                updated = dict(doc)
                # Preserve worker-owned fields that are not part of the API model.
                recovered = job.model_dump(mode="json")
                for field in ("status", "error", "retry_count", "started_at", "completed_at"):
                    updated[field] = recovered[field]
                store.replace_with_etag(updated, doc["_etag"])
                logger.info("Recovered job %s to %s (retry %d)", job.id, job.status.value, job.retry_count)
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            logger.debug("Job %s changed during recovery; skipping", doc.get("id"))
        except Exception:
            # One malformed document or failed write must not starve later jobs.
            logger.exception("Could not recover job %s", doc.get("id"))


# ── main loop ─────────────────────────────────────────────────────────────

async def process_active_pipelines():
    """One iteration: run health checks and job monitoring for active pipelines.

    NOTE: All source processing (change feed, blob enumeration, backfill) is
    handled by the .NET changefeed service + .NET worker via Service Bus.
    The Python controller only monitors health and stuck jobs.
    """
    pass  # Health checks and job monitoring handled in main loop


async def sync_docgrok_from_store():
    """Restore DocGrok transform pipelines and external models from CosmosDB on startup."""
    store = get_store()
    synced_p = 0
    synced_m = 0

    async with httpx.AsyncClient(timeout=15) as client:
        # Sync transform pipelines
        stored_pipelines = store.list("docgrok_pipeline")
        for doc in stored_pipelines:
            name = doc["id"]
            payload = {k: v for k, v in doc.items()
                       if k not in ("id", "doc_type", "stored_at") and not k.startswith("_")}
            try:
                check = await client.get(f"{DOCGROK_URL}/admin/pipelines/{name}")
                if check.status_code == 404:
                    resp = await client.post(f"{DOCGROK_URL}/admin/pipelines",
                                             json={**payload, "name": name})
                    if resp.status_code < 400:
                        synced_p += 1
                        logger.info("Synced pipeline '%s' to DocGrok", name)
                    else:
                        logger.warning("Failed to sync pipeline '%s': %s", name, resp.text)
            except Exception as e:
                logger.warning("Error syncing pipeline '%s': %s", name, e)

        # Sync external models — ensure DocGrok has the exact IDs from CosmosDB
        stored_models = store.list("docgrok_model")
        # Get current DocGrok registry to detect stale/wrong-ID models
        try:
            reg_resp = await client.get(f"{DOCGROK_URL}/admin/models/registry")
            dg_models = reg_resp.json().get("models", []) if reg_resp.status_code == 200 else []
        except Exception:
            dg_models = []
        dg_ext = {m["id"]: m for m in dg_models if m.get("kind") == "external"}
        stored_ids = {doc["id"] for doc in stored_models}

        # Delete DocGrok external models that don't match any stored ID (stale/wrong-ID)
        for dg_id in list(dg_ext.keys()):
            if dg_id not in stored_ids:
                try:
                    await client.delete(f"{DOCGROK_URL}/admin/models/registry/{dg_id}")
                    logger.info("Removed stale model '%s' from DocGrok", dg_id)
                except Exception as e:
                    logger.warning("Failed to remove stale model '%s': %s", dg_id, e)

        # Register/update stored models
        for doc in stored_models:
            model_id = doc["id"]
            payload = {k: v for k, v in doc.items()
                       if k not in ("doc_type", "stored_at") and not k.startswith("_")}
            try:
                if model_id not in dg_ext:
                    resp = await client.post(f"{DOCGROK_URL}/admin/models/registry", json=payload)
                    if resp.status_code < 400:
                        synced_m += 1
                        logger.info("Synced model '%s' (%s) to DocGrok", model_id, payload.get("name", ""))
                    else:
                        logger.warning("Failed to sync model '%s': %s", model_id, resp.text)
            except Exception as e:
                logger.warning("Error syncing model '%s': %s", model_id, e)

    logger.info("DocGrok sync complete: %d pipelines, %d models restored", synced_p, synced_m)


def restore_operational_config():
    """Read operational config from CosmosDB and apply to K8s on startup."""
    store = get_store()
    try:
        doc = store.get("operational-config", "config")
        if not doc:
            logger.info("No operational config found in CosmosDB, using defaults")
            return
    except Exception as e:
        logger.warning("Could not read operational config: %s", e)
        return

    from kubernetes import client, config as k8s_config
    k8s_config.load_incluster_config()
    apps_v1 = client.AppsV1Api()
    autoscaling_v2 = client.AutoscalingV2Api()
    ns = "omnivec"

    SETTINGS_MAP = {
        "changefeed.replicas":  {"deployment": "omnivec-changefeed", "hpa_field": "both"},
        "worker.minReplicas":   {"deployment": "omnivec-worker",     "hpa_field": "min"},
        "worker.maxReplicas":   {"deployment": "omnivec-worker",     "hpa_field": "max"},
        "controller.replicas":  {"deployment": "omnivec-controller", "hpa_field": None},
        "api.replicas":         {"deployment": "omnivec-api",        "hpa_field": None},
        "web.replicas":         {"deployment": "omnivec-web",        "hpa_field": None},
    }

    applied = []
    for key, schema in SETTINGS_MAP.items():
        value = doc.get(key)
        if value is None:
            continue
        dep_name = schema["deployment"]
        hpa_field = schema["hpa_field"]
        try:
            if hpa_field in ("both", "min", "max"):
                hpa_patch = {}
                if hpa_field == "both":
                    hpa_patch = {"spec": {"minReplicas": value, "maxReplicas": value}}
                elif hpa_field == "min":
                    hpa_patch = {"spec": {"minReplicas": value}}
                elif hpa_field == "max":
                    hpa_patch = {"spec": {"maxReplicas": value}}
                try:
                    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                        dep_name, ns, body=hpa_patch)
                except Exception:  # lgtm[py/empty-except]
                    pass
            if hpa_field in ("both", None):
                apps_v1.patch_namespaced_deployment_scale(
                    dep_name, ns, body={"spec": {"replicas": value}})
            applied.append(f"{key}={value}")
        except Exception as e:
            logger.warning("Failed to apply %s=%s: %s", key, value, e)

    if applied:
        logger.info("Restored operational config: %s", ", ".join(applied))


async def main():
    global _last_health_check
    logger.info("OmniVec Controller starting")
    init_store()
    logger.info("CosmosDB store initialized")

    # Restore operational config from CosmosDB
    try:
        restore_operational_config()
    except Exception as e:
        logger.error("Operational config restore failed: %s", e)

    # Sync DocGrok config from CosmosDB on startup
    try:
        await sync_docgrok_from_store()
    except Exception as e:
        logger.error("DocGrok startup sync failed: %s", e)

    while True:
        try:
            await process_active_pipelines()
            monitor_job_health()

            # Run health checks on interval
            now = datetime.utcnow()
            if _last_health_check is None or (now - _last_health_check).total_seconds() >= HEALTH_CHECK_INTERVAL:
                try:
                    await run_health_checks()
                    _last_health_check = now
                except Exception as he:
                    logger.error("Health check error: %s", he)
        except Exception as e:
            logger.error("Controller loop error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
