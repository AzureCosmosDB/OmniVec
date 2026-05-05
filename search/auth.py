"""Bearer-token auth + rate limiting for the OmniVec Search service.

Two-token model:
  - OMNIVEC_SEARCH_TOKEN   — bootstrap/root search token (env)
  - scope=search tokens    — issued via api.py /api/auth/tokens (stored in
                             Cosmos `metadata` container with scope="search")

Admin-scope tokens are REJECTED (403) unless SEARCH_ACCEPT_ADMIN_TOKEN=true.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

from azure.cosmos.exceptions import CosmosHttpResponseError

logger = logging.getLogger(__name__)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


_BOOTSTRAP_RAW = os.getenv("OMNIVEC_SEARCH_TOKEN", "")
BOOTSTRAP_HASH = _sha256(_BOOTSTRAP_RAW) if _BOOTSTRAP_RAW else ""
del _BOOTSTRAP_RAW

_INTERNAL_RAW = os.getenv("SEARCH_INTERNAL_TOKEN", "")
INTERNAL_HASH = _sha256(_INTERNAL_RAW) if _INTERNAL_RAW else ""
del _INTERNAL_RAW

ACCEPT_ADMIN = os.getenv("SEARCH_ACCEPT_ADMIN_TOKEN", "false").lower() == "true"
TOKENS_STORE_ENABLED = os.getenv("SEARCH_TOKENS_STORE_ENABLED", "true").lower() == "true"

COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT", "")
COSMOS_DATABASE = os.getenv("COSMOS_DATABASE", "omnivec")
COSMOS_METADATA_CONTAINER = os.getenv("COSMOS_METADATA_CONTAINER", "metadata")


# -----------------------------------------------------------------------------
# Cosmos container (lazy)
# -----------------------------------------------------------------------------

_container = None
_container_lock = threading.Lock()


def _get_container():
    global _container
    if _container is not None:
        return _container
    if not (TOKENS_STORE_ENABLED and COSMOS_ENDPOINT):
        return None
    with _container_lock:
        if _container is not None:
            return _container
        try:
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential
            client = CosmosClient(COSMOS_ENDPOINT, credential=DefaultAzureCredential())
            db = client.get_database_client(COSMOS_DATABASE)
            _container = db.get_container_client(COSMOS_METADATA_CONTAINER)
            return _container
        except Exception as e:
            # Client construction failed (bad endpoint, missing credential).
            # We intentionally soft-fail so the API stays serviceable on its
            # bootstrap admin token; log loudly so operators see the issue.
            logger.error(
                "Auth-token Cosmos init failed — token-store lookups disabled: %s",
                e,
            )
            return None


def _lookup_token_in_store(token_hash: str) -> Optional[dict]:
    c = _get_container()
    if c is None:
        return None
    try:
        items = c.query_items(
            query=(
                "SELECT * FROM c WHERE c.doc_type = 'auth_token' "
                "AND c.token_hash = @hash"
            ),
            parameters=[{"name": "@hash", "value": token_hash}],
            enable_cross_partition_query=True,
        )
        for t in items:
            if not secrets.compare_digest(t.get("token_hash", ""), token_hash):
                continue
            if t.get("revoked"):
                return None
            if t.get("expires_at"):
                try:
                    if datetime.fromisoformat(t["expires_at"]) < datetime.utcnow():
                        return None
                except ValueError as ve:
                    logger.warning(
                        "Malformed expires_at on auth_token: %s", ve,
                    )
            return t
    except CosmosHttpResponseError as exc:
        # Cosmos transient/permanent failure. We return None so the caller
        # falls back to the bootstrap token path rather than hard-failing
        # every request, but log at ERROR so a monitoring alert fires on
        # a persistent Cosmos outage.
        logger.error(
            "auth_token lookup Cosmos error (status=%s): %s",
            getattr(exc, "status_code", None), exc,
        )
    except Exception as e:
        # Catch-all for unexpected client-library errors. Still logged.
        logger.error("auth_token lookup unexpected error: %s", e)
    return None


# -----------------------------------------------------------------------------
# AuthResult + validation
# -----------------------------------------------------------------------------


class AuthResult:
    __slots__ = ("subject", "scope", "source", "token_id", "rate_limit_override")

    def __init__(
        self,
        subject: str,
        scope: str,
        source: str,
        token_id: str = "",
        rate_limit_override: Optional[int] = None,
    ):
        self.subject = subject
        self.scope = scope
        self.source = source
        self.token_id = token_id
        # RES-4: optional per-token override of SEARCH_RATE_LIMIT_RPM. None
        # = use global; 0 = uncapped; positive int = override.
        self.rate_limit_override = rate_limit_override

    def __repr__(self) -> str:
        return f"AuthResult(subject={self.subject!r}, scope={self.scope!r}, source={self.source!r})"

    @property
    def rate_limit_key(self) -> str:
        """RES-4: per-token bucket key. Falls back to subject when token id
        is empty (env-bootstrap / api-internal). Two tokens with the same
        ``name`` no longer share a bucket — they have distinct ids."""
        return f"tok:{self.token_id}" if self.token_id else f"sub:{self.subject}"


def validate_token(token: str) -> Optional[AuthResult]:
    """Return AuthResult if token is valid for the search service, else None.

    Admin-scope tokens are REJECTED unless SEARCH_ACCEPT_ADMIN_TOKEN=true.
    """
    if not token:
        return None
    h = _sha256(token)

    if BOOTSTRAP_HASH and secrets.compare_digest(h, BOOTSTRAP_HASH):
        return AuthResult(subject="bootstrap", scope="search", source="env")

    if INTERNAL_HASH and secrets.compare_digest(h, INTERNAL_HASH):
        return AuthResult(subject="api-internal", scope="search", source="env")

    t = _lookup_token_in_store(h)
    if t is None:
        return None

    # scope field preferred; fall back to legacy `role` for back-compat
    scope = (t.get("scope") or t.get("role") or "").lower()
    if scope == "admin" and not ACCEPT_ADMIN:
        logger.info("Rejecting admin-scope token for search (token_id=%s)", t.get("id"))
        return None
    if scope not in ("search", "admin"):
        return None
    # RES-4: per-token rate-limit override. Tokens with a `rate_limit_rpm`
    # field override the SEARCH_RATE_LIMIT_RPM global. Use 0 to remove the
    # limit entirely for trusted automation tokens; positive values clamp
    # the limit further. Negative values are ignored (fall back to global).
    raw_limit = t.get("rate_limit_rpm")
    try:
        limit_override = int(raw_limit) if raw_limit is not None else None
        if limit_override is not None and limit_override < 0:
            limit_override = None
    except (TypeError, ValueError):
        limit_override = None
    return AuthResult(
        subject=t.get("name", ""),
        scope=scope,
        source="store",
        token_id=t.get("id", ""),
        rate_limit_override=limit_override,
    )


# -----------------------------------------------------------------------------
# Rate limiting (in-memory token bucket per subject)
# -----------------------------------------------------------------------------


RATE_LIMIT_RPM = int(os.getenv("SEARCH_RATE_LIMIT_RPM", "0"))  # 0 = disabled
_rate_window_s = 60.0
_rate_state: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


def check_rate_limit(subject: str, *, override_rpm: Optional[int] = None) -> bool:
    """Return True if request is allowed, False if rate limited.

    RES-4: callers pass the auth result's ``rate_limit_key`` (per-token
    bucket) and optional ``override_rpm`` (per-token override). The
    override has these semantics:
      * ``None``           — fall back to the global ``RATE_LIMIT_RPM``.
      * ``0``              — uncapped (trusted automation).
      * positive integer   — clamp to that limit.
    """
    effective = RATE_LIMIT_RPM if override_rpm is None else override_rpm
    if effective <= 0:
        return True
    now = time.time()
    with _rate_lock:
        dq = _rate_state[subject]
        while dq and now - dq[0] > _rate_window_s:
            dq.popleft()
        if len(dq) >= effective:
            return False
        dq.append(now)
        return True
