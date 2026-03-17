#!/usr/bin/env python3
"""Blob Controller - Single controller for ALL blob sources.

Responsibilities:
- Monitors all active blob sources
- Creates/scales/deletes per-source worker deployments
- Tracks progress across all sources
- Coordinates backfill → live transitions
- Handles stuck job recovery

Uses leader election for HA - only one instance active at a time.
All state stored in CosmosDB - crash resilient.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from azure.cosmos.exceptions import CosmosAccessConditionFailedError
from models import Source, SourceType, Pipeline, Job, JobStatus
from store import init_store, get_store
from progress_tracker import ProgressTracker, SourceStatus
from leader_election import run_with_leader_election

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [blob-controller] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for _logger in ("azure.core", "azure.identity", "urllib3", "kubernetes"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

# Configuration
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "30"))
STUCK_JOB_TIMEOUT = int(os.environ.get("STUCK_JOB_TIMEOUT", "300"))
LEASE_NAME = os.environ.get("LEASE_NAME", "blob-controller-leader")
NAMESPACE = os.environ.get("NAMESPACE", "omnivec")

# Kubernetes client (lazy init)
_k8s_apps = None
_k8s_autoscaling = None


def _get_k8s_clients():
    """Get Kubernetes API clients."""
    global _k8s_apps, _k8s_autoscaling
    if _k8s_apps is None:
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            _k8s_apps = client.AppsV1Api()
            _k8s_autoscaling = client.AutoscalingV2Api()
        except Exception as e:
            logger.warning("Kubernetes not available: %s", e)
    return _k8s_apps, _k8s_autoscaling


def _strip_doc(doc: dict) -> dict:
    """Remove CosmosDB system fields."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class BlobController:
    """Controller for all blob sources."""

    def __init__(self):
        self.store = None
        self._active_sources: Dict[str, Source] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []  # Track tasks for graceful shutdown

    async def start(self):
        """Start the controller."""
        logger.info("Blob Controller starting")
        init_store()
        self.store = get_store()
        self._running = True

        # Run main loops with task tracking for graceful shutdown
        self._tasks = [
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._stuck_job_recovery_loop(), name="stuck_recovery"),
            asyncio.create_task(self._progress_update_loop(), name="progress"),
        ]
        await asyncio.gather(*self._tasks)

    async def stop(self):
        """Stop the controller gracefully."""
        logger.info("Blob Controller stopping")
        self._running = False

        # Cancel all running tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to complete cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def _reconcile_loop(self):
        """Main reconciliation loop - ensure deployments match desired state."""
        while self._running:
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Reconcile error: %s", e)

            await asyncio.sleep(RECONCILE_INTERVAL)

    async def _reconcile(self):
        """Reconcile deployments with desired state."""
        # Get all blob sources with active pipelines
        active_sources = await self._get_active_blob_sources()

        # Get current deployments
        current_deployments = await self._get_current_deployments()

        # Create missing deployments
        for source_id, source in active_sources.items():
            if source_id not in self._active_sources:
                logger.info("New source detected: %s (%s)", source_id, source.name)
                await self._create_source_deployments(source)
                self._active_sources[source_id] = source

            # Update progress tracker status
            tracker = ProgressTracker(source_id)
            if f"blob-backfill-{source_id}" not in current_deployments:
                tracker.set_status(SourceStatus.STARTING, "Creating workers")

        # Delete deployments for removed sources
        for source_id in list(self._active_sources.keys()):
            if source_id not in active_sources:
                logger.info("Source removed: %s", source_id)
                await self._delete_source_deployments(source_id)
                del self._active_sources[source_id]

        # Scale deployments based on load
        for source_id, source in active_sources.items():
            await self._scale_source_workers(source)

    async def _get_active_blob_sources(self) -> Dict[str, Source]:
        """Get all blob sources that have active pipelines."""
        # Get all active pipelines
        pipeline_query = (
            "SELECT * FROM c WHERE c.doc_type = 'pipeline' "
            "AND c.is_active = true"
        )
        pipeline_docs = self.store.query(pipeline_query, [], partition_key="pipeline")

        # Collect source IDs from active pipelines
        active_source_ids = set()
        for doc in pipeline_docs:
            for sid in doc.get("source_ids", []):
                active_source_ids.add(sid)

        # Get source details
        sources = {}
        for source_id in active_source_ids:
            try:
                doc = self.store.get(source_id, partition_key="source")
                source = Source(**_strip_doc(doc))
                if source.type == SourceType.AZURE_BLOB:
                    sources[source_id] = source
            except Exception as e:
                logger.warning("Could not load source %s: %s", source_id, e)

        return sources

    async def _get_current_deployments(self) -> Dict[str, Any]:
        """Get current blob worker deployments."""
        apps, _ = _get_k8s_clients()
        if not apps:
            return {}

        deployments = {}
        try:
            dep_list = await asyncio.to_thread(
                apps.list_namespaced_deployment,
                NAMESPACE,
                label_selector="managed-by=omnivec-blob-controller"
            )
            for dep in dep_list.items:
                deployments[dep.metadata.name] = {
                    "replicas": dep.spec.replicas,
                    "ready": dep.status.ready_replicas or 0,
                    "source": dep.metadata.labels.get("source", ""),
                }
        except Exception as e:
            logger.warning("Could not list deployments: %s", e)

        return deployments

    async def _create_source_deployments(self, source: Source):
        """Create backfill and live worker deployments for a source."""
        apps, autoscaling = _get_k8s_clients()
        if not apps:
            logger.warning("Cannot create deployments - Kubernetes not available")
            return

        source_id = source.id
        backfill_config = source.config.get("backfill", {})
        live_config = source.config.get("live", {})

        # Create backfill deployment
        if backfill_config.get("enabled", True):
            await self._create_deployment(
                name=f"blob-backfill-{source_id}",
                source_id=source_id,
                command=["python", "-m", "blob_backfill_worker"],
                replicas=backfill_config.get("workers_min", 1),
                env={
                    "SOURCE_ID": source_id,
                    "CHECKPOINT_INTERVAL": str(backfill_config.get("checkpoint_interval", 100)),
                    "BATCH_SIZE": str(backfill_config.get("batch_size", 50)),
                },
            )

            # Create HPA for backfill
            await self._create_hpa(
                name=f"blob-backfill-{source_id}",
                deployment_name=f"blob-backfill-{source_id}",
                min_replicas=backfill_config.get("workers_min", 1),
                max_replicas=backfill_config.get("workers_max", 20),
            )

        # Create live deployment
        if live_config.get("enabled", True):
            await self._create_deployment(
                name=f"blob-live-{source_id}",
                source_id=source_id,
                command=["python", "-m", "blob_live_worker"],
                replicas=live_config.get("workers_min", 1),
                env={
                    "SOURCE_ID": source_id,
                    "QUEUE_NAME": live_config.get("storage_queue", f"blob-events-{source_id}"),
                    "VISIBILITY_TIMEOUT": "300",
                },
            )

            # Create HPA for live
            await self._create_hpa(
                name=f"blob-live-{source_id}",
                deployment_name=f"blob-live-{source_id}",
                min_replicas=live_config.get("workers_min", 1),
                max_replicas=live_config.get("workers_max", 10),
            )

        logger.info("Created deployments for source %s", source_id)

    async def _create_deployment(
        self,
        name: str,
        source_id: str,
        command: List[str],
        replicas: int,
        env: Dict[str, str],
    ):
        """Create a Kubernetes deployment."""
        apps, _ = _get_k8s_clients()
        if not apps:
            return

        from kubernetes import client

        # Build environment variables
        env_vars = [
            client.V1EnvVar(name=k, value=v) for k, v in env.items()
        ]
        # Add common env vars
        env_vars.extend([
            client.V1EnvVar(
                name="AZURE_CLIENT_ID",
                value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(
                        field_path="metadata.annotations['azure.workload.identity/client-id']"
                    )
                )
            ),
        ])

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
                labels={
                    "app": name.rsplit("-", 1)[0],  # blob-backfill or blob-live
                    "source": source_id,
                    "managed-by": "omnivec-blob-controller",
                },
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(
                    match_labels={
                        "app": name.rsplit("-", 1)[0],
                        "source": source_id,
                    }
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app": name.rsplit("-", 1)[0],
                            "source": source_id,
                            "azure.workload.identity/use": "true",
                        }
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="omnivec-api",
                        containers=[
                            client.V1Container(
                                name="worker",
                                image=os.environ.get(
                                    "WORKER_IMAGE",
                                    "omnivecregistry.azurecr.io/omnivec-api:v1"
                                ),
                                command=command,
                                env=env_vars,
                                resources=client.V1ResourceRequirements(
                                    requests={"memory": "1Gi", "cpu": "500m"},
                                    limits={"memory": "2Gi", "cpu": "2"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        try:
            await asyncio.to_thread(
                apps.create_namespaced_deployment,
                NAMESPACE,
                deployment
            )
        except Exception as e:
            if "AlreadyExists" in str(e):
                # Update instead
                await asyncio.to_thread(
                    apps.patch_namespaced_deployment,
                    name,
                    NAMESPACE,
                    deployment
                )
            else:
                raise

    async def _create_hpa(
        self,
        name: str,
        deployment_name: str,
        min_replicas: int,
        max_replicas: int,
    ):
        """Create HorizontalPodAutoscaler."""
        _, autoscaling = _get_k8s_clients()
        if not autoscaling:
            return

        from kubernetes import client

        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
            ),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=deployment_name,
                ),
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=70,
                            ),
                        ),
                    )
                ],
            ),
        )

        try:
            await asyncio.to_thread(
                autoscaling.create_namespaced_horizontal_pod_autoscaler,
                NAMESPACE,
                hpa
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                logger.warning("Could not create HPA %s: %s", name, e)

    async def _delete_source_deployments(self, source_id: str):
        """Delete all deployments for a source."""
        apps, autoscaling = _get_k8s_clients()
        if not apps:
            return

        for prefix in ["blob-backfill", "blob-live"]:
            name = f"{prefix}-{source_id}"
            try:
                await asyncio.to_thread(
                    apps.delete_namespaced_deployment,
                    name,
                    NAMESPACE
                )
            except Exception:
                pass

            try:
                await asyncio.to_thread(
                    autoscaling.delete_namespaced_horizontal_pod_autoscaler,
                    name,
                    NAMESPACE
                )
            except Exception:
                pass

        logger.info("Deleted deployments for source %s", source_id)

    async def _scale_source_workers(self, source: Source):
        """Scale workers based on pending job count."""
        # This is handled by HPA based on CPU
        # Additional logic could scale based on pending jobs via KEDA
        pass

    async def _stuck_job_recovery_loop(self):
        """Recover stuck jobs periodically."""
        while self._running:
            try:
                await self._recover_stuck_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Stuck job recovery error: %s", e)

            await asyncio.sleep(60)  # Check every minute

    async def _recover_stuck_jobs(self):
        """Find and reset stuck PROCESSING jobs."""
        cutoff = (datetime.utcnow() - timedelta(seconds=STUCK_JOB_TIMEOUT)).isoformat()

        query = (
            "SELECT * FROM c WHERE c.doc_type = 'job' "
            "AND c.status = 'processing' "
            "AND c.started_at < @cutoff"
        )
        params = [{"name": "@cutoff", "value": cutoff}]

        stuck_jobs = self.store.query(query, params, partition_key="job")

        if stuck_jobs:
            logger.info("Found %d potentially stuck jobs", len(stuck_jobs))

        reset_count = 0
        for doc in stuck_jobs:
            job = Job(**_strip_doc(doc))
            etag = doc.get("_etag")

            # Check retry count
            if job.retry_count >= 3:
                doc["status"] = JobStatus.FAILED.value
                doc["error"] = "Max retries exceeded (stuck in processing)"
                doc["completed_at"] = datetime.utcnow().isoformat()
            else:
                doc["status"] = JobStatus.PENDING.value
                doc["started_at"] = None
                doc["retry_count"] = job.retry_count + 1

            try:
                # Use etag to ensure job is still in PROCESSING state
                # This prevents resetting a job that just completed
                self.store.replace_with_etag(doc, etag)
                reset_count += 1
            except CosmosAccessConditionFailedError:
                # Job was updated (likely completed) - skip
                logger.debug("Job %s was updated, skipping reset", job.id)
            except Exception as e:
                logger.error("Failed to reset stuck job %s: %s", job.id, e)

        if reset_count > 0:
            logger.info("Reset %d stuck jobs", reset_count)

    async def _progress_update_loop(self):
        """Update progress documents periodically."""
        while self._running:
            try:
                await self._update_all_progress()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Progress update error: %s", e)

            await asyncio.sleep(30)

    async def _update_all_progress(self):
        """Update progress for all active sources."""
        for source_id, source in self._active_sources.items():
            try:
                await self._update_source_progress(source_id)
            except Exception as e:
                logger.warning("Could not update progress for %s: %s", source_id, e)

    async def _update_source_progress(self, source_id: str):
        """Update progress document for a source."""
        # Count jobs by status
        count_query = (
            "SELECT c.status, COUNT(1) as count FROM c "
            "WHERE c.doc_type = 'job' AND c.source_id = @source_id "
            "GROUP BY c.status"
        )
        params = [{"name": "@source_id", "value": source_id}]

        results = self.store.query(count_query, params, partition_key="job")

        pending = 0
        processing = 0
        completed = 0
        failed = 0

        for r in results:
            status = r.get("status", "")
            count = r.get("count", 0)
            if status == "pending":
                pending = count
            elif status == "processing":
                processing = count
            elif status == "completed":
                completed = count
            elif status == "failed":
                failed = count

        # Update progress
        tracker = ProgressTracker(source_id)
        tracker.update_backfill_progress(
            location="default",
            blobs_enumerated=completed + pending + processing + failed,
            jobs_created=completed + pending + processing + failed,
            jobs_completed=completed,
            jobs_failed=failed,
            jobs_pending=pending + processing,
        )

        # Update worker status
        apps, _ = _get_k8s_clients()
        if apps:
            for worker_type in ["backfill", "live"]:
                try:
                    dep = await asyncio.to_thread(
                        apps.read_namespaced_deployment,
                        f"blob-{worker_type}-{source_id}",
                        NAMESPACE
                    )
                    tracker.update_workers(
                        worker_type=worker_type,
                        desired=dep.spec.replicas or 0,
                        ready=dep.status.ready_replicas or 0,
                        processing=dep.status.available_replicas or 0,
                    )
                except Exception:
                    pass


async def main():
    """Main entry point."""
    controller = BlobController()

    async def run_controller():
        await controller.start()

    # Run with leader election
    await run_with_leader_election(
        lease_name=LEASE_NAME,
        main_func=run_controller,
        namespace=NAMESPACE,
    )


if __name__ == "__main__":
    asyncio.run(main())
