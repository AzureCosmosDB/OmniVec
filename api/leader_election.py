#!/usr/bin/env python3
"""Leader Election - Kubernetes Lease-based leader election.

Ensures only one instance of a controller is active at a time.
Uses Kubernetes Lease API for distributed coordination.
"""

import os
import sys
import logging
import asyncio
import socket
import random
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# Try to import kubernetes, fall back gracefully
try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    logger.warning("kubernetes package not available - leader election disabled")


class LeaderElector:
    """
    Kubernetes Lease-based leader election.

    Only one instance will be the leader at any time.
    If the leader dies, another instance will take over.
    """

    def __init__(
        self,
        lease_name: str,
        namespace: str = "omnivec",
        lease_duration_seconds: int = 15,
        renew_deadline_seconds: int = 10,
        retry_period_seconds: int = 2,
    ):
        """
        Args:
            lease_name: Name of the Lease resource (e.g., "blob-controller-leader")
            namespace: Kubernetes namespace
            lease_duration_seconds: How long the lease is valid
            renew_deadline_seconds: How long to try renewing before giving up
            retry_period_seconds: How often to retry acquiring lease
        """
        self.lease_name = lease_name
        self.namespace = namespace
        self.lease_duration = lease_duration_seconds
        self.renew_deadline = renew_deadline_seconds
        self.retry_period = retry_period_seconds

        # Identity of this instance
        self.identity = os.environ.get("HOSTNAME", socket.gethostname())

        self._is_leader = False
        self._stop_event = asyncio.Event()
        self._on_started_leading: Optional[Callable[[], Awaitable[None]]] = None
        self._on_stopped_leading: Optional[Callable[[], Awaitable[None]]] = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3  # Only give up after 3 consecutive failures

        # Initialize Kubernetes client
        self._coord_api = None
        if K8S_AVAILABLE:
            try:
                config.load_incluster_config()
                self._coord_api = client.CoordinationV1Api()
            except Exception:
                try:
                    config.load_kube_config()
                    self._coord_api = client.CoordinationV1Api()
                except Exception as e:
                    logger.warning("Could not load Kubernetes config: %s", e)

    @property
    def is_leader(self) -> bool:
        """Check if this instance is currently the leader."""
        return self._is_leader

    def on_started_leading(self, callback: Callable[[], Awaitable[None]]):
        """Register callback when this instance becomes leader."""
        self._on_started_leading = callback

    def on_stopped_leading(self, callback: Callable[[], Awaitable[None]]):
        """Register callback when this instance loses leadership."""
        self._on_stopped_leading = callback

    async def run(self):
        """Run the leader election loop."""
        if not K8S_AVAILABLE or not self._coord_api:
            # CRITICAL: Do not assume leadership without proper coordination
            # This could lead to multiple leaders corrupting shared state
            logger.error(
                "Kubernetes not available - leader election requires K8S. Exiting."
            )
            sys.exit(1)

        logger.info(
            "Starting leader election for %s/%s as %s",
            self.namespace, self.lease_name, self.identity
        )

        while not self._stop_event.is_set():
            try:
                if self._is_leader:
                    # Try to renew
                    renewed = await self._try_renew_lease()
                    if not renewed:
                        logger.warning("Lost leadership for %s", self.lease_name)
                        self._is_leader = False
                        if self._on_stopped_leading:
                            await self._on_stopped_leading()
                else:
                    # Try to acquire
                    acquired = await self._try_acquire_lease()
                    if acquired:
                        logger.info("Acquired leadership for %s", self.lease_name)
                        self._is_leader = True
                        if self._on_started_leading:
                            await self._on_started_leading()

            except Exception as e:
                logger.error("Leader election error: %s", e)
                self._consecutive_failures += 1

                # Only give up leadership after multiple consecutive failures
                # This prevents transient network errors from causing unnecessary failover
                if self._is_leader and self._consecutive_failures >= self._max_consecutive_failures:
                    logger.warning(
                        "Lost leadership after %d consecutive failures",
                        self._consecutive_failures
                    )
                    self._is_leader = False
                    if self._on_stopped_leading:
                        await self._on_stopped_leading()

            # Wait with jitter to prevent thundering herd
            jitter = random.uniform(0, self.retry_period * 0.2)
            await asyncio.sleep(self.retry_period + jitter)

    async def stop(self):
        """Stop the leader election loop."""
        self._stop_event.set()
        if self._is_leader:
            self._is_leader = False
            if self._on_stopped_leading:
                await self._on_stopped_leading()

    async def _try_acquire_lease(self) -> bool:
        """Try to acquire or take over the lease."""
        try:
            now = datetime.now(timezone.utc)

            # Try to get existing lease
            try:
                lease = await asyncio.to_thread(
                    self._coord_api.read_namespaced_lease,
                    self.lease_name,
                    self.namespace
                )

                # Check if lease is expired
                if lease.spec.renew_time:
                    renew_time = lease.spec.renew_time
                    if renew_time.tzinfo is None:
                        renew_time = renew_time.replace(tzinfo=timezone.utc)

                    elapsed = (now - renew_time).total_seconds()
                    if elapsed < self.lease_duration:
                        # Lease is still valid, owned by someone else
                        if lease.spec.holder_identity != self.identity:
                            return False

                # Lease expired or we already own it - take it
                # CRITICAL: Preserve resourceVersion for optimistic concurrency
                # This prevents race condition where two pods both see expired lease
                resource_version = lease.metadata.resource_version  # lgtm[py/unused-local-variable]

                lease.spec.holder_identity = self.identity
                lease.spec.lease_duration_seconds = self.lease_duration
                lease.spec.acquire_time = now
                lease.spec.renew_time = now

                await asyncio.to_thread(
                    self._coord_api.replace_namespaced_lease,
                    self.lease_name,
                    self.namespace,
                    lease
                )
                self._consecutive_failures = 0  # Reset on success
                return True

            except ApiException as e:
                if e.status == 404:
                    # Lease doesn't exist, create it
                    lease = client.V1Lease(
                        metadata=client.V1ObjectMeta(
                            name=self.lease_name,
                            namespace=self.namespace,
                        ),
                        spec=client.V1LeaseSpec(
                            holder_identity=self.identity,
                            lease_duration_seconds=self.lease_duration,
                            acquire_time=now,
                            renew_time=now,
                        )
                    )
                    await asyncio.to_thread(
                        self._coord_api.create_namespaced_lease,
                        self.namespace,
                        lease
                    )
                    self._consecutive_failures = 0  # Reset on success
                    return True
                raise

        except ApiException as e:
            if e.status == 409:
                # Conflict - someone else got it
                return False
            raise
        except Exception as e:
            logger.error("Error acquiring lease: %s", e)
            return False

    async def _try_renew_lease(self) -> bool:
        """Try to renew the lease."""
        try:
            now = datetime.now(timezone.utc)

            lease = await asyncio.to_thread(
                self._coord_api.read_namespaced_lease,
                self.lease_name,
                self.namespace
            )

            # Verify we still own it
            if lease.spec.holder_identity != self.identity:
                return False

            # Renew - resourceVersion is preserved in lease.metadata for optimistic concurrency
            lease.spec.renew_time = now

            await asyncio.to_thread(
                self._coord_api.replace_namespaced_lease,
                self.lease_name,
                self.namespace,
                lease
            )
            self._consecutive_failures = 0  # Reset on success
            return True

        except ApiException as e:
            if e.status in (404, 409):
                return False
            raise
        except Exception as e:
            logger.error("Error renewing lease: %s", e)
            return False


async def run_with_leader_election(
    lease_name: str,
    main_func: Callable[[], Awaitable[None]],
    namespace: str = "omnivec",
):
    """
    Run a function only when this instance is the leader.

    The function will be started when leadership is acquired
    and cancelled when leadership is lost.
    """
    elector = LeaderElector(lease_name, namespace)
    main_task: Optional[asyncio.Task] = None

    async def on_started():
        nonlocal main_task
        logger.info("Starting main function as leader")
        main_task = asyncio.create_task(main_func())

    async def on_stopped():
        nonlocal main_task
        if main_task and not main_task.done():
            logger.info("Stopping main function - lost leadership")
            main_task.cancel()
            try:
                await main_task
            except asyncio.CancelledError:  # lgtm[py/empty-except]
                pass
            main_task = None

    elector.on_started_leading(on_started)
    elector.on_stopped_leading(on_stopped)

    try:
        await elector.run()
    finally:
        await elector.stop()
        if main_task and not main_task.done():
            main_task.cancel()
