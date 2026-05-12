"""OmniVec Search service — FastAPI entrypoint.

Standalone, externally-reachable multi-index vector-search API.
Not coupled to api.py. External consumers and the Playground UI (via nginx
proxy) hit this service directly.
"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from auth import (
    ACCEPT_ADMIN,
    RATE_LIMIT_RPM,
    check_rate_limit,
    validate_token,
)
from schemas import SearchRequest, SearchResponse
from searcher import (
    DOCGROK_URL,
    PER_INDEX_TIMEOUT_S,
    TOTAL_TIMEOUT_S,
    explain_search,
    run_search,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("omnivec.search")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    logger.info(
        "search-service up: docgrok=%s per_idx_timeout=%ss total_timeout=%ss rate_limit_rpm=%s accept_admin=%s",
        DOCGROK_URL, PER_INDEX_TIMEOUT_S, TOTAL_TIMEOUT_S, RATE_LIMIT_RPM, ACCEPT_ADMIN,
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="OmniVec Search",
    description="Standalone multi-index vector search API.",
    version="1.0.0",
    lifespan=lifespan,
)


# CR/LF/control-char scrubber on root logger — mitigates py/log-injection.
import re as _re

class _CtrlCharLogFilter(logging.Filter):
    _CTRL_RE = _re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\r\n|\r|\n')

    @classmethod
    def _scrub(cls, v):
        return cls._CTRL_RE.sub(' ', v) if isinstance(v, str) else v

    def filter(self, record):
        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


logging.getLogger().addFilter(_CtrlCharLogFilter())
for _h in logging.getLogger().handlers:
    _h.addFilter(_CtrlCharLogFilter())


_cors_origins = [o.strip() for o in os.getenv("SEARCH_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def _require_search_token(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token: Optional[str] = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide Authorization: Bearer <search-token>",
        )
    result = validate_token(token)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired search token")
    if result.scope != "search":
        raise HTTPException(
            status_code=403,
            detail=f"Token scope '{result.scope}' cannot access the search API",
        )
    if not check_rate_limit(result.rate_limit_key, override_rpm=result.rate_limit_override):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    request.state.auth = result
    return result


# -----------------------------------------------------------------------------
# Open routes
# -----------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "service": "omnivec-search"}


@app.get("/ready")
async def ready(request: Request):
    try:
        resp = await request.app.state.http.get(f"{DOCGROK_URL}/health", timeout=3.0)
        if resp.status_code == 200:
            return {"status": "ready", "docgrok": "healthy"}
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "docgrok": f"status={resp.status_code}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "docgrok_error": str(e)[:200]},  # lgtm[py/stack-trace-exposure]
        )


@app.get("/schema")
async def schema():
    """JSON schema of the /search request body (for client codegen)."""
    return SearchRequest.model_json_schema()


# -----------------------------------------------------------------------------
# Authenticated routes
# -----------------------------------------------------------------------------


@app.post("/search", response_model=SearchResponse)
async def search_endpoint(req: SearchRequest, request: Request):
    _require_search_token(request)
    rid = req.request_id or f"srv-{uuid.uuid4().hex[:12]}"
    req.request_id = rid
    logger.info(
        "search rid=%s q_len=%s n_indexes=%s top_k=%s merge=%s",
        rid, len(req.query or ""), len(req.indexes), req.top_k, req.merge.strategy,  # lgtm[py/log-injection]
    )
    try:
        return await run_search(request.app.state.http, req)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="search total timeout exceeded")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("strict=true"):
            raise HTTPException(status_code=422, detail=msg)
        raise HTTPException(status_code=502, detail=msg)


@app.post("/search/explain")
async def search_explain(req: SearchRequest, request: Request):
    _require_search_token(request)
    return await explain_search(req)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
