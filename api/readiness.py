"""Cached, coalesced metadata readiness checks, separate from process liveness."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class StorageReadiness:
    def __init__(self, probe, *, timeout=3.0, cache_seconds=5.0):
        self._probe = probe
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._task = None
        self._checked_at = None
        self._ready = False

    async def _check(self):
        try:
            await asyncio.to_thread(self._probe)
        except Exception as error:
            logger.warning("Metadata readiness probe failed: %s", type(error).__name__)
            self._ready = False
        else:
            self._ready = True
        self._checked_at = time.monotonic()

    async def ready(self):
        if self._task is not None and not self._task.done():
            task = self._task
        elif (self._checked_at is not None
              and time.monotonic() - self._checked_at < self._cache_seconds):
            return self._ready
        else:
            task = self._task = asyncio.create_task(self._check())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning("Metadata readiness probe exceeded its deadline")
            return False
        return self._ready
