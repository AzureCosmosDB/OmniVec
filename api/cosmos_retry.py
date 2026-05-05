"""Cosmos DB retry helper — shared across api/ and search/.

Provides a decorator that retries Cosmos operations on transient errors
(429/408/5xx) with exponential backoff, honoring the server-recommended
`x-ms-retry-after-ms` / `Retry-After` header when present.

Semantic errors (NotFound, ResourceExists, EtagMismatch) are NEVER retried —
they indicate caller-visible state and must bubble up so callers can react.
"""
from __future__ import annotations

import logging
import random
import time
from functools import wraps
from typing import Callable, Optional

from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

logger = logging.getLogger(__name__)

# Transient HTTP statuses worth retrying.
_RETRYABLE_STATUS = {408, 429, 449, 500, 502, 503, 504}

# Exceptions that indicate caller-observable state — never retry these.
_NON_RETRYABLE_EXCEPTIONS = (
    CosmosResourceNotFoundError,
    CosmosResourceExistsError,
    CosmosAccessConditionFailedError,
)

DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_BASE_DELAY = 0.2
DEFAULT_MAX_DELAY = 30.0


def _retry_after_seconds(exc: CosmosHttpResponseError) -> Optional[float]:
    """Extract the server-recommended retry delay from a Cosmos error.

    The Azure Cosmos SDK exposes response headers inconsistently across
    versions; probe both the attached response and the exception itself.
    """
    candidates = []
    resp = getattr(exc, "response", None)
    if resp is not None:
        candidates.append(getattr(resp, "headers", None))
    candidates.append(getattr(exc, "headers", None))

    for headers in candidates:
        if not headers:
            continue
        try:
            ms = headers.get("x-ms-retry-after-ms") or headers.get("retry-after-ms")
            if ms:
                return float(ms) / 1000.0
            s = headers.get("Retry-After") or headers.get("retry-after")
            if s:
                return float(s)
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def cosmos_retry(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
):
    """Retry a Cosmos call on 429/408/5xx with exponential backoff + jitter.

    The server's `x-ms-retry-after-ms` / `Retry-After` header is honored
    when present; otherwise we fall back to exponential backoff with a
    cap of `max_delay` seconds. Semantic errors are re-raised immediately.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except _NON_RETRYABLE_EXCEPTIONS:
                    raise
                except CosmosHttpResponseError as exc:
                    status = getattr(exc, "status_code", None)
                    if status not in _RETRYABLE_STATUS or attempt >= max_attempts:
                        logger.error(
                            "Cosmos %s gave up after %d attempt(s): status=%s",
                            func.__name__, attempt, status,
                        )
                        raise
                    delay = _retry_after_seconds(exc)
                    if delay is None:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        # Full jitter (capped at 25%) to avoid herds on
                        # synchronized retries across many workers.
                        delay += random.uniform(0, delay * 0.25)
                    else:
                        delay = min(delay, max_delay)
                    logger.warning(
                        "Cosmos %s attempt %d/%d failed status=%s; retrying in %.2fs",
                        func.__name__, attempt, max_attempts, status, delay,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator


__all__ = ["cosmos_retry"]
