"""Auth for the OmniVec Agent service.

The agent never trusts callers directly — it sits behind the api proxy
(``api/api.py``) which validates the caller's JWT / admin token and forwards
identity via ``X-Caller-Id`` + ``X-Caller-Role``. The proxy authenticates
itself to the agent with a shared ``INTERNAL_API_TOKEN`` (same pattern as
the existing X-Admin-Token internal hop between api/search/docgrok).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request


VALID_ROLES = {"reader", "admin"}


@dataclass(frozen=True)
class CallerIdentity:
    caller_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _internal_token() -> str:
    return os.environ.get("INTERNAL_API_TOKEN", "")


def require_internal_caller(
    request: Request,
    authorization: str | None = Header(default=None),
    x_caller_id: str | None = Header(default=None),
    x_caller_role: str | None = Header(default=None),
) -> CallerIdentity:
    """FastAPI dependency that enforces:

      1. ``Authorization: Bearer <INTERNAL_API_TOKEN>`` (constant-time compare)
      2. ``X-Caller-Id`` non-empty
      3. ``X-Caller-Role`` in {reader, admin}

    Skips ``/v1/health`` and ``/v1/ready`` (those are k8s probe-friendly).
    """
    path = request.url.path
    if path in ("/v1/health", "/v1/ready"):
        return CallerIdentity(caller_id="probe", role="reader")

    expected = _internal_token()
    if not expected:
        raise HTTPException(status_code=503, detail="agent: INTERNAL_API_TOKEN not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing internal bearer")
    presented = authorization[len("Bearer "):]
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid internal bearer")

    if not x_caller_id:
        raise HTTPException(status_code=401, detail="missing X-Caller-Id")
    role = (x_caller_role or "").strip().lower()
    if role not in VALID_ROLES:
        role = "reader"

    return CallerIdentity(caller_id=x_caller_id, role=role)
