#!/usr/bin/env python3
"""CosmosDB Controller - Single controller for ALL CosmosDB sources.

Responsibilities:
- Monitors all active CosmosDB sources
- Creates/scales/deletes per-source changefeed + backfill deployments
- Tracks partition progress and lag
- Coordinates backfill completion
- Handles stuck job recovery

Uses leader election for HA - only one instance active at a time.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta  # lgtm[py/unused-import]
from typing import Dict, List, Optional, Any

from models import Source, SourceType, Pipeline, Job, JobStatus  # lgtm[py/unused-import]
from store import init_store, get_store
from progress_tracker import ProgressTracker, SourceStatus
from leader_election import run_with_leader_election

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cosmosdb-controller] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for _logger in ("azure.core", "azure.identity", "urllib3", "kubernetes"):
    logging.getLogger(_logger).setLevel(logging.WARNING)

# Configuration
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "30"))
STUCK_JOB_TIMEOUT = int(os.environ.get("STUCK_JOB_TIMEOUT", "300"))
LEASE_NAME = os.environ.get("LEASE_NAME", "cosmosdb-controller-leader")
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
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class CosmosDBController:
    """Controller for all CosmosDB sources."""

    def __init__(self):
        self.store = None
        self._active_sources: Dict[str, Source] = {}
        self._running = False

    async def start(self):
        """Start the controller."""
        logger.info("CosmosDB Controller starting")
        init_store()
        self.store = get_store()
        self._running = True

        await asyncio.gather(
            self._reconcile_loop(),
            self._progress_update_loop(),
        )

    async def stop(self):
        """Stop the controller."""
        logger.info("CosmosDB Controller stopping")
        self._running = False

    async def _reconcile_loop(self):
        """Main reconciliation loop."""
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
        # Get all CosmosDB sources with active pipelines
        active_sources = await self._get_active_cosmosdb_sources()

        # Get current deployments
        current_deployments = await self._get_current_deployments()  # lgtm[py/unused-local-variable]

        # Create missing deployments
        for source_id, source in active_sources.items():
            if source_id not in self._active_sources:
                logger.info("New CosmosDB source detected: %s (%s)", source_id, source.name)
                await self._create_source_deployments(source)
                self._active_sources[source_id] = source

        # Delete deployments for removed sources
        for source_id in list(self._active_sources.keys()):
            if source_id not in active_sources:
                logger.info("CosmosDB source removed: %s", source_id)
                await self._delete_source_deployments(source_id)
                del self._active_sources[source_id]

    async def _get_active_cosmosdb_sources(self) -> Dict[str, Source]:
        """Get all CosmosDB sources with active pipelines."""
        # Get active pipelines
        pipeline_query = (
            "SELECT * FROM c WHERE c.doc_type = 'pipeline' "
            "AND c.is_active = true"
        )
        pipeline_docs = self.store.query(pipeline_query, [], partition_key="pipeline")

        # Collect source IDs
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
                if source.type == SourceType.COSMOSDB:
                    sources[source_id] = source
            except Exception as e:
                logger.warning("Could not load source %s: %s", source_id, e)

        return sources

    async def _get_current_deployments(self) -> Dict[str, Any]:
        """Get current CosmosDB worker deployments."""
        apps, _ = _get_k8s_clients()
        if not apps:
            return {}

        deployments = {}
        try:
            dep_list = await asyncio.to_thread(
                apps.list_namespaced_deployment,
                NAMESPACE,
                label_selector="managed-by=omnivec-cosmosdb-controller"
            )
            for dep in dep_list.items:
                deployments[dep.metadata.name] = {
                    "replicas": dep.spec.replicas,
                    "ready": dep.status.ready_replicas or 0,
                }
        except Exception as e:
            logger.warning("Could not list deployments: %s", e)

        return deployments

    async def _create_source_deployments(self, source: Source):
        """Create changefeed and backfill deployments for a source."""
        apps, _ = _get_k8s_clients()
        if not apps:
            logger.warning("Cannot create deployments - Kubernetes not available")
            return

        source_id = source.id
        changefeed_config = source.config.get("changefeed", {})
        backfill_config = source.config.get("backfill", {})

        # Create changefeed deployment
        if changefeed_config.get("enabled", True):
            replicas = changefeed_config.get("workers", 15)
            await self._create_deployment(
                name=f"cosmos-cf-{source_id}",
                source_id=source_id,
                image=os.environ.get(
                    "CHANGEFEED_IMAGE",
                    "omnivecregistry.azurecr.io/omnivec-changefeed:v1"
                ),
                replicas=replicas,
                env={
                    "SOURCE_ID": source_id,
                    "PROCESSING_MODE": changefeed_config.get("processing_mode", "inline"),
                },
                app_label="cosmos-cf",
            )

        # Create backfill deployment (if needed)
        if backfill_config.get("enabled", True):
            await self._create_deployment(
                name=f"cosmos-backfill-{source_id}",
                source_id=source_id,
                image=os.environ.get(
                    "WORKER_IMAGE",
                    "omnivecregistry.azurecr.io/omnivec-api:v1"
                ),
                command=["python", "-m", "cosmosdb_backfill_worker"],
                replicas=backfill_config.get("workers_min", 1),
                env={
                    "SOURCE_ID": source_id,
                    "PAGE_SIZE": str(backfill_config.get("page_size", 1000)),
                    "CHECKPOINT_INTERVAL": str(backfill_config.get("checkpoint_interval", 100)),
                },
                app_label="cosmos-backfill",
            )

            # Create HPA for backfill
            await self._create_hpa(
                name=f"cosmos-backfill-{source_id}",
                deployment_name=f"cosmos-backfill-{source_id}",
                min_replicas=backfill_config.get("workers_min", 1),
                max_replicas=backfill_config.get("workers_max", 10),
            )

        logger.info("Created CosmosDB deployments for source %s", source_id)

        # Update progress tracker
        tracker = ProgressTracker(source_id)
        tracker.set_status(SourceStatus.STARTING, "Creating workers")

    async def _create_deployment(
        self,
        name: str,
        source_id: str,
        image: str,
        replicas: int,
        env: Dict[str, str],
        app_label: str,
        command: Optional[List[str]] = None,
    ):
        """Create a Kubernetes deployment."""
        apps, _ = _get_k8s_clients()
        if not apps:
            return

        from kubernetes import client

        env_vars = [client.V1EnvVar(name=k, value=v) for k, v in env.items()]

        container_spec = {
            "name": "worker",
            "image": image,
            "env": env_vars,
            "resources": client.V1ResourceRequirements(
                requests={"memory": "512Mi", "cpu": "250m"},
                limits={"memory": "1Gi", "cpu": "1"},
            ),
        }
        if command:
            container_spec["command"] = command

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=NAMESPACE,
                labels={
                    "app": app_label,
                    "source": source_id,
                    "managed-by": "omnivec-cosmosdb-controller",
                },
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(
                    match_labels={"app": app_label, "source": source_id}
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app": app_label,
                            "source": source_id,
                            "azure.workload.identity/use": "true",
                        }
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="omnivec-api",
                        containers=[client.V1Container(**container_spec)],
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
        """Create HPA."""
        _, autoscaling = _get_k8s_clients()
        if not autoscaling:
            return

        from kubernetes import client

        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE),
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

        for prefix in ["cosmos-cf", "cosmos-backfill"]:
            name = f"{prefix}-{source_id}"
            try:
                await asyncio.to_thread(
                    apps.delete_namespaced_deployment,
                    name,
                    NAMESPACE
                )
            except Exception:  # lgtm[py/empty-except]
                pass

            if prefix == "cosmos-backfill":
                try:
                    await asyncio.to_thread(
                        autoscaling.delete_namespaced_horizontal_pod_autoscaler,
                        name,
                        NAMESPACE
                    )
                except Exception:  # lgtm[py/empty-except]
                    pass

        logger.info("Deleted CosmosDB deployments for source %s", source_id)

    async def _progress_update_loop(self):
        """Update progress for all sources."""
        while self._running:
            try:
                for source_id in self._active_sources:
                    await self._update_source_progress(source_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Progress update error: %s", e)

            await asyncio.sleep(30)

    async def _update_source_progress(self, source_id: str):
        """Update progress for a CosmosDB source."""
        # For CosmosDB, progress is mostly tracked by the changefeed processor
        # Here we just check worker health
        apps, _ = _get_k8s_clients()
        if not apps:
            return

        tracker = ProgressTracker(source_id)

        for worker_type in ["cf", "backfill"]:
            try:
                dep = await asyncio.to_thread(
                    apps.read_namespaced_deployment,
                    f"cosmos-{worker_type}-{source_id}",
                    NAMESPACE
                )
                tracker.update_workers(
                    worker_type=f"cosmos-{worker_type}",
                    desired=dep.spec.replicas or 0,
                    ready=dep.status.ready_replicas or 0,
                    processing=dep.status.available_replicas or 0,
                )
            except Exception:  # lgtm[py/empty-except]
                pass


async def main():
    controller = CosmosDBController()

    async def run_controller():
        await controller.start()

    await run_with_leader_election(
        lease_name=LEASE_NAME,
        main_func=run_controller,
        namespace=NAMESPACE,
    )


if __name__ == "__main__":
    asyncio.run(main())
