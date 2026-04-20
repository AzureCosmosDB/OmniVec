#!/usr/bin/env python3
"""OmniVec Control Plane API"""

import os
import json
import uuid
import time
import asyncio
import logging
import hashlib
import secrets
import httpx
import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

# Initialize telemetry (in-memory MetricsStore always active; App Insights if configured)
try:
    from telemetry import (
        init_telemetry, metrics_store,
        record_embedding_batch, record_search, record_error,
        record_request, record_failure,
        track_metric, track_histogram, track_event, Timer,
    )
    init_telemetry()
except ImportError:
    # Telemetry module not available — define no-ops
    def track_metric(*a, **kw): pass
    def track_histogram(*a, **kw): pass
    def track_event(*a, **kw): pass
    def record_embedding_batch(**kw): pass
    def record_search(**kw): pass
    def record_error(**kw): pass
    def record_request(**kw): pass
    def record_failure(**kw): pass
    metrics_store = None
    class Timer:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

from models import (
    Source, Destination, Pipeline, Job, JobStatus, JobStats,
    CreateSourceRequest, CreateDestinationRequest, CreatePipelineRequest,
    SyncSourceRequest, PipelineRunStats, PipelineStatus, SourceType,
    ModelCategory, Assistant, CreateAssistantRequest, AssistantChatRequest
)
from store import init_store, get_store

logger = logging.getLogger(__name__)

# Filter sensitive data from logs
import re
class _SensitiveFilter(logging.Filter):
    _patterns = [
        (re.compile(r'(api[_-]?key|password|secret|token|credential)[=:]\s*["\']?([^\s"\'&,}{]{8})[^\s"\'&,}{]*', re.I), r'\1=\2***'),
        (re.compile(r'(Bearer\s+)(\S{8})\S+', re.I), r'\1\2***'),
    ]
    def filter(self, record):
        msg = str(record.msg)
        for pattern, replacement in self._patterns:
            msg = pattern.sub(replacement, msg)
        record.msg = msg
        return True

logging.getLogger().addFilter(_SensitiveFilter())
# Suppress verbose Azure SDK logs
logging.getLogger("azure").setLevel(logging.WARNING)

_DEBUG = os.getenv("OMNIVEC_DEBUG", "").lower() in ("true", "1")
app = FastAPI(
    title="OmniVec", version="1.0.0", description="Universal Vector Ingestion Platform",
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    openapi_url="/openapi.json" if _DEBUG else None,
)

# =============================================================================
# AUTHENTICATION â€” Bearer Token
# =============================================================================

# Paths that don't require authentication
AUTH_SKIP_PATHS = {"/health", "/health/", "/openapi.json", "/docs", "/redoc"}
AUTH_SKIP_PREFIXES = ("/static/",)

# Admin bootstrap token â€” hash immediately, never keep plaintext in memory
_ADMIN_TOKEN_RAW = os.getenv("OMNIVEC_ADMIN_TOKEN", "")
ADMIN_TOKEN_HASH = hashlib.sha256(_ADMIN_TOKEN_RAW.encode()).hexdigest() if _ADMIN_TOKEN_RAW else ""
del _ADMIN_TOKEN_RAW  # Remove plaintext from memory


def _hash_token(token: str) -> str:
    """SHA-256 hash a token for storage (never store plaintext)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _validate_token(token: str) -> Optional[dict]:
    """Validate a bearer token. Returns token metadata or None.

    Uses constant-time comparison to prevent timing attacks.
    """
    token_hash = _hash_token(token)
    # Check admin bootstrap token (constant-time comparison)
    if ADMIN_TOKEN_HASH and secrets.compare_digest(token_hash, ADMIN_TOKEN_HASH):
        return {"name": "admin", "role": "admin", "created_by": "env"}
    # Check stored tokens in CosmosDB â€” query by hash for efficiency
    try:
        store = get_store()
        tokens = store.query(
            "SELECT * FROM c WHERE c.doc_type = 'auth_token' AND c.token_hash = @hash",
            parameters=[{"name": "@hash", "value": token_hash}],
            partition_key="auth_token",
        )
        for t in tokens:
            if not secrets.compare_digest(t.get("token_hash", ""), token_hash):
                continue
            if t.get("revoked"):
                return None
            if t.get("expires_at"):
                if datetime.fromisoformat(t["expires_at"]) < datetime.utcnow():
                    return None
            return {"name": t.get("name", ""), "role": t.get("role", "user"), "id": t.get("id", "")}
    except Exception as e:
        logger.warning("Token validation error: %s", e)
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all /api/* and /ui requests."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for health, static, and auth endpoints
        if path in AUTH_SKIP_PATHS or any(path.startswith(p) for p in AUTH_SKIP_PREFIXES):
            return await call_next(request)

        # /api/auth/login is public (validates token and returns metadata)
        if path == "/api/auth/login":
            client_ip = request.client.host if request.client else "unknown"
            if not _check_rate_limit(client_ip):
                return JSONResponse(status_code=429, content={"detail": "Too many authentication attempts. Try again later."})
            return await call_next(request)

        # All /api/* and /ui require auth
        if path.startswith("/api/") or path == "/ui":
            # Skip auth for internal cluster calls â€” exact K8s DNS match only
            _INTERNAL_HOSTS = {
                "omnivec-api", "omnivec-api:80",
                "omnivec-api.omnivec", "omnivec-api.omnivec:80",
                "omnivec-api.omnivec.svc", "omnivec-api.omnivec.svc:80",
                "omnivec-api.omnivec.svc.cluster.local", "omnivec-api.omnivec.svc.cluster.local:80",
            }
            host = request.headers.get("Host", "").lower().strip()
            if host in _INTERNAL_HOSTS:
                request.state.internal = True
                return await call_next(request)

            auth_header = request.headers.get("Authorization", "")
            token = None

            if auth_header.startswith("Bearer "):
                token = auth_header[7:]

            if not token:
                # For /ui, redirect-style: serve login page instead
                if path == "/ui":
                    return await call_next(request)  # UI handles auth check client-side
                return JSONResponse(status_code=401, content={"detail": "Authentication required. Provide Authorization: Bearer <token>"})

            token_meta = _validate_token(token)
            if not token_meta:
                # Count failed auth attempts for rate limiting
                client_ip = request.client.host if request.client else "unknown"
                if not _check_rate_limit(client_ip):
                    return JSONResponse(status_code=429, content={"detail": "Too many authentication attempts. Try again later."})
                if path == "/ui":
                    return await call_next(request)
                return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})

            # Attach token metadata to request state
            request.state.auth = token_meta

        return await call_next(request)


app.add_middleware(AuthMiddleware)

# Rate limiter for auth endpoints â€” prevent brute force token guessing
_auth_attempts: dict[str, list[float]] = {}  # ip -> [timestamps]
_AUTH_RATE_LIMIT = 10  # max attempts per window
_AUTH_RATE_WINDOW = 60  # seconds

def _check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate limited."""
    import time
    now = time.time()
    attempts = _auth_attempts.get(client_ip, [])
    attempts = [t for t in attempts if now - t < _AUTH_RATE_WINDOW]
    if len(attempts) >= _AUTH_RATE_LIMIT:
        _auth_attempts[client_ip] = attempts
        return False
    attempts.append(now)
    _auth_attempts[client_ip] = attempts
    return True

# CORS â€” restrict origins. Set CORS_ORIGINS env var for cross-origin access.
# Default: no cross-origin allowed (UI is same-origin via nginx proxy).
from fastapi.middleware.cors import CORSMiddleware
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

# Security headers on all API responses
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Track request latency and error rates for all API calls."""
    start = time.time()
    response = await call_next(request)
    latency_ms = (time.time() - start) * 1000
    path = request.url.path
    # Skip metrics/health endpoints to avoid recursion noise
    if not path.startswith("/api/metrics") and not path.startswith("/api/health"):
        record_request(latency_ms, method=request.method, path=path)
        if response.status_code >= 400:
            record_error(status_code=response.status_code, path=path)
    return response

# â”€â”€ test-connection timeout / retry config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TEST_CONN_TIMEOUT = 10   # seconds
TEST_CONN_RETRIES = 1


_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=10)


def _build_mssql_odbc_conn_str(cfg: dict) -> str:
    """Build an ODBC connection string for MS SQL from config dict."""
    cs = cfg.get("connection_string", "")
    if cs:
        # Convert ADO.NET format to ODBC if needed
        if "Driver=" not in cs and "DRIVER=" not in cs:
            parts = {}
            for part in cs.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    parts[k.strip().lower()] = v.strip()
            server = parts.get("server", parts.get("host", parts.get("data source", "")))
            database = parts.get("database", parts.get("initial catalog", ""))
            user = parts.get("username", parts.get("user id", parts.get("uid", "")))
            password = parts.get("password", parts.get("pwd", ""))
            cs = f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Uid={user};Pwd={password};Encrypt=yes;TrustServerCertificate=yes;"
        return cs
    server = cfg.get("server", cfg.get("host", ""))
    database = cfg.get("database", "")
    user = cfg.get("user", cfg.get("username", ""))
    password = cfg.get("password", "")
    return f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Uid={user};Pwd={password};Encrypt=yes;TrustServerCertificate=yes;"


def _discover_mssql_vector_columns(cursor, table: str, schema: str = "dbo", config: dict = None) -> list:
    """Discover vector columns in an MSSQL table.

    Probes INFORMATION_SCHEMA for native ``vector`` type columns (SQL Server 2025+).
    Falls back to reporting the configured ``vector_column`` if it exists in the
    table — older SQL Server stores vectors as JSON in NVARCHAR columns.

    Returns ``vector_indexes`` in the same format as CosmosDB so the UI dropdown
    and pipeline ``vector_index_path`` validation work identically.
    """
    import re as _re
    cursor.execute(
        """SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
           FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
           ORDER BY ORDINAL_POSITION""",
        (table, schema),
    )
    columns = cursor.fetchall()

    vector_indexes = []
    all_col_names = set()
    for col_name, data_type, max_length in columns:
        all_col_names.add(col_name)
        dt_lower = (data_type or "").lower()
        # SQL Server 2025+ native vector type
        if "vector" in dt_lower:
            dimensions = None
            if max_length and isinstance(max_length, int) and max_length > 0:
                dimensions = max_length
            vector_indexes.append({
                "path": col_name,
                "dimensions": dimensions,
                "dataType": "vector",
            })

    # Fallback: if no native vector columns found, include the configured
    # vector_column when it exists in the table (NVARCHAR storing JSON vectors)
    if not vector_indexes and config:
        vec_col = config.get("vector_column", config.get("vector_col", "embedding"))
        if vec_col in all_col_names:
            vector_indexes.append({
                "path": vec_col,
                "dimensions": config.get("vector_dimensions"),
                "dataType": "float32",
            })

    return vector_indexes


async def _test_with_timeout(fn, retries: int = TEST_CONN_RETRIES, timeout: int = TEST_CONN_TIMEOUT):
    """Run a test-connection function with timeout and retries.
    fn can be sync or async â€” sync calls run in a thread pool so timeout works.
    Returns (success: bool, result_or_error)."""
    last_err = None
    loop = asyncio.get_event_loop()
    for attempt in range(1, retries + 1):
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_thread_pool, fn),
                timeout=timeout,
            )
            return True, result
        except asyncio.TimeoutError:
            last_err = f"Connection timed out after {timeout}s (attempt {attempt}/{retries})"
            logger.warning("Test connection timeout attempt %d/%d", attempt, retries)
        except Exception as e:
            last_err = str(e)
            logger.warning("Test connection error attempt %d/%d: %s", attempt, retries, e)
            break  # non-timeout errors: no point retrying
    return False, last_err

# =============================================================================
# COSMOS DB STORE HELPERS
# =============================================================================

# Sensitive fields that must never be returned in API responses
_SENSITIVE_CONFIG_KEYS = {
    "password", "api_key", "secret", "connection_string", "access_key",
    "shared_key", "sas_token", "token", "client_secret",
}

def _mask_config(config: dict) -> dict:
    """Mask sensitive fields in source/destination config for API responses."""
    if not config:
        return config
    masked = {}
    for k, v in config.items():
        if any(s in k.lower() for s in _SENSITIVE_CONFIG_KEYS):
            masked[k] = "***" if v else v
        else:
            masked[k] = v
    return masked

def _source_from_doc(doc: dict, mask: bool = True) -> Source:
    """Convert a CosmosDB document to a Source model."""
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)  # remove partition key discriminator
    if mask and "config" in d and isinstance(d["config"], dict):
        d["config"] = _mask_config(d["config"])
    return Source(**d)

def _destination_from_doc(doc: dict, mask: bool = True) -> Destination:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    if mask and "config" in d and isinstance(d["config"], dict):
        d["config"] = _mask_config(d["config"])
    return Destination(**d)

def _pipeline_from_doc(doc: dict) -> Pipeline:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return Pipeline(**d)

def _job_from_doc(doc: dict) -> Job:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return Job(**d)

def _to_doc(model: BaseModel, doc_type: str) -> dict:
    """Convert a Pydantic model to a CosmosDB document with doc_type discriminator."""
    doc = model.model_dump(mode="json")
    doc["doc_type"] = doc_type
    return doc

# Event processing queue
EVENT_QUEUE: asyncio.Queue = None

# =============================================================================
# CONFIGURATION
# =============================================================================

DOCGROK_URL = os.getenv("DOCGROK_URL", "http://docgrok:80")

# HTTP Client
http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global http_client, EVENT_QUEUE
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30),
    )
    EVENT_QUEUE = asyncio.Queue()
    # Initialize CosmosDB store
    try:
        init_store()
        print("OmniVec API started - CosmosDB store initialized")
    except Exception as e:
        print(f"WARNING: CosmosDB store init failed ({e}). API will fail on data operations.")
    # Start the event processor worker
    asyncio.create_task(event_processor_worker())
    print("OmniVec API started - Event processor initialized")


@app.on_event("shutdown")
async def shutdown():
    global http_client
    if http_client:
        await http_client.aclose()
    print("OmniVec API shutdown")


# =============================================================================
# STATIC FILES
# =============================================================================

# Check multiple possible locations for static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(STATIC_DIR):
    STATIC_DIR = os.path.join(os.path.dirname(__file__), "web", "static")
if not os.path.exists(STATIC_DIR):
    STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/ui")
async def serve_ui():
    """Serve the OmniVec web UI."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="UI not found")


# =============================================================================
# HEALTH
# =============================================================================

@app.get("/health")
async def health():
    """Lightweight health check for K8s probes â€” no external calls."""
    return {"status": "healthy", "service": "OmniVec", "version": "1.0.0"}


# =============================================================================
# AUTH ENDPOINTS
# =============================================================================

class CreateTokenRequest(BaseModel):
    name: str
    role: str = "user"  # "admin" or "user"
    expires_days: Optional[int] = None  # None = no expiry


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Validate a token and return metadata. Public endpoint."""
    body = await request.json()
    token = body.get("token", "")
    if not token:
        raise HTTPException(status_code=400, detail="Token required")
    meta = _validate_token(token)
    if not meta:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"authenticated": True, **meta}


@app.post("/api/auth/tokens")
async def create_token(req: CreateTokenRequest, request: Request):
    """Generate a new access token. Requires admin role."""
    auth = getattr(request.state, "auth", None)
    if not auth or auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to create tokens")

    token = secrets.token_urlsafe(32)
    token_id = f"tok-{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow().isoformat()

    doc = {
        "id": token_id,
        "doc_type": "auth_token",
        "name": req.name,
        "role": req.role,
        "token_hash": _hash_token(token),
        "created_at": now,
        "created_by": auth.get("name", "unknown"),
        "revoked": False,
    }
    if req.expires_days:
        doc["expires_at"] = (datetime.utcnow() + timedelta(days=req.expires_days)).isoformat()

    store = get_store()
    store.upsert(doc)

    return {
        "id": token_id,
        "name": req.name,
        "role": req.role,
        "token": token,  # Only shown once at creation time
        "expires_at": doc.get("expires_at"),
        "message": "Save this token â€” it cannot be retrieved again."
    }


@app.get("/api/auth/tokens")
async def list_tokens(request: Request):
    """List all tokens (without the actual token values). Requires admin role."""
    auth = getattr(request.state, "auth", None)
    if not auth or auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    store = get_store()
    tokens = store.query("SELECT * FROM c WHERE c.doc_type = 'auth_token'")
    return {"tokens": [
        {
            "id": t["id"],
            "name": t.get("name"),
            "role": t.get("role"),
            "created_at": t.get("created_at"),
            "created_by": t.get("created_by"),
            "expires_at": t.get("expires_at"),
            "revoked": t.get("revoked", False),
        }
        for t in tokens
    ]}


@app.delete("/api/auth/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request):
    """Revoke a token. Requires admin role."""
    auth = getattr(request.state, "auth", None)
    if not auth or auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    store = get_store()
    doc = store.get(token_id, "auth_token")
    if not doc:
        raise HTTPException(status_code=404, detail="Token not found")
    doc["revoked"] = True
    store.upsert(doc)
    return {"message": f"Token {token_id} revoked"}


@app.get("/api/stats")
async def get_stats():
    """Detailed stats endpoint (used by UI dashboard, not by probes)."""
    docgrok_status = "unknown"
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/health", timeout=5.0)
        if resp.status_code == 200:
            docgrok_status = "healthy"
        else:
            docgrok_status = f"unhealthy ({resp.status_code})"
    except Exception as e:
        docgrok_status = f"error: {str(e)}"

    store = get_store()
    metrics_doc = await asyncio.to_thread(store.get, "global", "metrics")
    events_processed = metrics_doc.get("events_processed", 0) if metrics_doc else 0
    events_failed = metrics_doc.get("events_failed", 0) if metrics_doc else 0

    def _count(doc_type):
        result = store.query(
            "SELECT VALUE COUNT(1) FROM c WHERE c.doc_type = @dt",
            [{"name": "@dt", "value": doc_type}],
            partition_key=doc_type
        )
        return result[0] if result else 0

    src_count, dst_count, pip_count, job_stats = await asyncio.gather(
        asyncio.to_thread(_count, "source"),
        asyncio.to_thread(_count, "destination"),
        asyncio.to_thread(_count, "pipeline"),
        asyncio.to_thread(get_job_stats),
    )

    return {
        "status": "healthy",
        "service": "OmniVec",
        "version": "1.0.0",
        "docgrok": docgrok_status,
        "stats": {
            "sources": src_count,
            "destinations": dst_count,
            "pipelines": pip_count,
            "events_processed": events_processed,
            "events_failed": events_failed,
            "jobs": job_stats.model_dump()
        }
    }


@app.get("/api/health/checks")
async def get_health_checks():
    """Get latest health check results from the controller."""
    store = get_store()
    doc = await asyncio.to_thread(store.get, "health_status", "health")
    if not doc:
        return {"overall": "unknown", "checked_at": None, "summary": {}, "sources": [], "destinations": [], "pipelines": [], "models": []}
    # Strip CosmosDB system fields
    return {k: v for k, v in doc.items() if not k.startswith("_") and k != "doc_type"}


@app.post("/api/health/checks/run")
async def run_health_checks_now(section: str | None = None):
    """Trigger an immediate health check run. Optional section: sources, destinations, pipelines, models."""
    from health_checker import run_health_checks
    if section and section not in ("sources", "destinations", "pipelines", "models"):
        raise HTTPException(status_code=400, detail=f"Invalid section '{section}'. Must be: sources, destinations, pipelines, models")
    result = await run_health_checks(section=section)
    return {k: v for k, v in result.items() if not k.startswith("_") and k != "doc_type"}


# =============================================================================
# METRICS — powered by Azure App Insights (Log Analytics)
# =============================================================================

def _get_logs_client():
    """Get a LogsQueryClient for querying App Insights."""
    workspace_id = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "")
    if not workspace_id:
        return None, None
    try:
        from azure.monitor.query import LogsQueryClient
        from azure.identity import DefaultAzureCredential
        return LogsQueryClient(DefaultAzureCredential()), workspace_id
    except Exception as e:
        logger.warning(f"Failed to create LogsQueryClient: {e}")
        return None, None


def _run_kql(kql: str, timespan=None):
    """Run a KQL query against Log Analytics. Returns rows or None."""
    client, ws_id = _get_logs_client()
    if not client:
        return None
    try:
        from azure.monitor.query import LogsQueryStatus
        resp = client.query_workspace(ws_id, kql, timespan=timespan)
        if resp.status == LogsQueryStatus.SUCCESS and resp.tables:
            return resp.tables[0].rows
        return []
    except Exception as e:
        logger.warning(f"KQL query failed: {e}")
        return None


@app.get("/api/metrics")
async def get_metrics():
    """Get processing metrics from App Insights.

    Queries customMetrics for totals, latency, throughput.
    Returns empty data if App Insights not configured.
    """
    kql = """
    let embedded = customMetrics
        | where name == 'omnivec.documents.embedded'
        | summarize total = sum(value);
    let failed = customMetrics
        | where name == 'omnivec.documents.failed'
        | summarize total = sum(value);
    let skipped = customMetrics
        | where name == 'omnivec.documents.skipped'
        | summarize total = sum(value);
    let searches = customMetrics
        | where name == 'omnivec.search.queries'
        | summarize total = sum(value);
    let tokens = customMetrics
        | where name == 'omnivec.tokens.used'
        | summarize total = sum(value);
    let errors = customMetrics
        | where name == 'omnivec.api.errors'
        | summarize total = sum(value);
    let embed_lat = customMetrics
        | where name == 'omnivec.embedding.latency'
        | summarize avg_ms = avg(value), p95_ms = percentile(value, 95), cnt = count();
    let search_lat = customMetrics
        | where name == 'omnivec.search.latency'
        | summarize avg_ms = avg(value), p95_ms = percentile(value, 95), cnt = count();
    let req_lat = customMetrics
        | where name == 'omnivec.request.latency'
        | summarize avg_ms = avg(value), p95_ms = percentile(value, 95), cnt = count();
    let throughput = customMetrics
        | where name == 'omnivec.documents.embedded' and timestamp > ago(1m)
        | summarize docs = sum(value);
    embedded | project metric='embedded', val=total
    | union (failed | project metric='failed', val=total)
    | union (skipped | project metric='skipped', val=total)
    | union (searches | project metric='searches', val=total)
    | union (tokens | project metric='tokens', val=total)
    | union (errors | project metric='errors', val=total)
    | union (embed_lat | project metric='embed_lat_avg', val=avg_ms)
    | union (embed_lat | project metric='embed_lat_p95', val=p95_ms)
    | union (embed_lat | project metric='embed_lat_cnt', val=cnt)
    | union (search_lat | project metric='search_lat_avg', val=avg_ms)
    | union (search_lat | project metric='search_lat_p95', val=p95_ms)
    | union (req_lat | project metric='req_lat_avg', val=avg_ms)
    | union (req_lat | project metric='req_lat_p95', val=p95_ms)
    | union (throughput | project metric='throughput_1m', val=docs)
    """

    rows = await asyncio.to_thread(_run_kql, kql, timedelta(days=7))

    if rows is None:
        # App Insights not configured — fallback to CosmosDB inline metrics
        try:
            store = get_store()
            doc = store.get("global", "metrics")
            if doc and doc.get("pipelines"):
                total_processed = doc.get("events_processed", 0)
                total_failed = doc.get("events_failed", 0)
                total_time_ms = 0.0
                for pdata in doc["pipelines"].values():
                    total_time_ms += pdata.get("total_time_ms", 0.0)
                avg_lat = round(total_time_ms / total_processed, 1) if total_processed > 0 else None
                return {
                    "events_processed": total_processed,
                    "events_failed": total_failed,
                    "avg_processing_time_ms": avg_lat,
                    "throughput_docs_per_sec": 0,
                    "search_queries": 0,
                    "latency": {"embedding": {"avg": avg_lat, "p95": None}, "search": {}, "request": {}},
                    "tokens": {"total": 0},
                    "skipped": {"total": 0},
                    "errors": {"total": total_failed},
                    "source": "cosmos_inline",
                }
        except Exception:
            pass
        return {
            "events_processed": 0,
            "events_failed": 0,
            "avg_processing_time_ms": None,
            "throughput_docs_per_sec": 0,
            "search_queries": 0,
            "latency": {"embedding": {}, "search": {}, "request": {}},
            "tokens": {"total": 0},
            "skipped": {"total": 0},
            "errors": {"total": 0},
            "source": "unavailable",
        }

    m = {}
    for row in rows:
        m[row[0]] = row[1] or 0

    throughput_1m = m.get("throughput_1m", 0)

    return {
        "events_processed": int(m.get("embedded", 0)),
        "events_failed": int(m.get("failed", 0)),
        "avg_processing_time_ms": round(m.get("embed_lat_avg", 0), 1) if m.get("embed_lat_cnt", 0) > 0 else None,
        "throughput_docs_per_sec": round(throughput_1m / 60, 2) if throughput_1m > 0 else 0,
        "search_queries": int(m.get("searches", 0)),
        "latency": {
            "embedding": {"avg": round(m.get("embed_lat_avg", 0), 1) if m.get("embed_lat_cnt", 0) > 0 else None, "p95": round(m.get("embed_lat_p95", 0), 1) if m.get("embed_lat_cnt", 0) > 0 else None},
            "search": {"avg": round(m.get("search_lat_avg", 0), 1) if m.get("search_lat_avg") else None, "p95": round(m.get("search_lat_p95", 0), 1) if m.get("search_lat_p95") else None},
            "request": {"avg": round(m.get("req_lat_avg", 0), 1) if m.get("req_lat_avg") else None, "p95": round(m.get("req_lat_p95", 0), 1) if m.get("req_lat_p95") else None},
        },
        "tokens": {"total": int(m.get("tokens", 0))},
        "skipped": {"total": int(m.get("skipped", 0))},
        "errors": {"total": int(m.get("errors", 0))},
        "source": "app_insights",
    }


@app.get("/api/metrics/insights")
def get_insights_metrics():
    """App Insights status + portal link."""
    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if not conn_str:
        return {"enabled": False, "message": "Application Insights not configured. Deploy with azd up to enable."}

    parts = dict(p.split("=", 1) for p in conn_str.split(";") if "=" in p)
    ikey = parts.get("InstrumentationKey", "")
    return {
        "enabled": True,
        "instrumentation_key": ikey[:8] + "..." if len(ikey) > 8 else ikey,
        "portal_url": f"https://portal.azure.com/#blade/AppInsightsExtension/OverviewBlade/InstrumentationKey/{ikey}" if ikey else None,
    }


@app.delete("/api/metrics")
def clear_metrics():
    """Clear is not applicable — metrics are in App Insights."""
    return {"success": False, "message": "Metrics are stored in App Insights and cannot be cleared from here. Use Azure Portal to manage retention."}


@app.post("/api/metrics/changefeed")
def report_changefeed_metrics(payload: dict):
    """Report changefeed batch metrics from CFP.
    Payload: {source_id, pipeline_id, total, eligible, skipped_no_content,
              skipped_unchanged, jobs_created, tokens_used, latency_ms, partition}
    """
    source_id = payload.get("source_id", "unknown")
    pipeline_id = payload.get("pipeline_id", "")
    total = int(payload.get("total", 0))
    eligible = int(payload.get("eligible", 0))
    skipped_no_content = int(payload.get("skipped_no_content", 0))
    skipped_unchanged = int(payload.get("skipped_unchanged", 0))
    jobs_created = int(payload.get("jobs_created", 0))
    tokens_used = int(payload.get("tokens_used", 0))
    latency_ms = float(payload.get("latency_ms", 0))
    failed = int(payload.get("failed", 0))

    # Feed into unified metrics system (in-memory + App Insights)
    record_embedding_batch(
        pipeline_id=pipeline_id, docs_embedded=eligible, docs_failed=failed,
        docs_skipped_no_content=skipped_no_content,
        docs_skipped_unchanged=skipped_unchanged,
        jobs_created=jobs_created, tokens_used=tokens_used,
        latency_ms=latency_ms, source_id=source_id,
    )

    return {"ok": True}


@app.get("/api/metrics/changefeed")
async def get_changefeed_metrics():
    """Get changefeed metrics from App Insights."""
    kql = """
    customMetrics
    | where name in ('omnivec.documents.embedded', 'omnivec.documents.failed', 'omnivec.documents.skipped', 'omnivec.pipeline.jobs_created')
    | summarize
        embedded = sumif(value, name == 'omnivec.documents.embedded'),
        failed = sumif(value, name == 'omnivec.documents.failed'),
        skipped = sumif(value, name == 'omnivec.documents.skipped'),
        jobs = sumif(value, name == 'omnivec.pipeline.jobs_created')
    """
    rows = await asyncio.to_thread(_run_kql, kql, timedelta(days=7))
    if rows is None or not rows:
        return {"total_eligible": 0, "total_failed": 0, "total_skipped": 0, "total_jobs_created": 0, "source": "unavailable"}
    row = rows[0]
    return {
        "total_eligible": int(row[0] or 0),
        "total_failed": int(row[1] or 0),
        "total_skipped": int(row[2] or 0),
        "total_jobs_created": int(row[3] or 0),
        "source": "app_insights",
    }


# â”€â”€ timeseries metrics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _build_cosmos_metrics_buckets(granularity, gran_seconds, start_dt, end_dt, pipeline_id):
    """Build time-series buckets from CosmosDB-stored inline metrics when App Insights is unavailable."""
    from collections import defaultdict

    store = get_store()
    doc = store.get("global", "metrics")
    if not doc:
        return []

    pipelines_data = doc.get("pipelines", {})
    if not pipelines_data:
        return []

    # Collect all recent entries from matching pipelines
    entries = []
    for pid, pdata in pipelines_data.items():
        if pipeline_id and pid != pipeline_id:
            continue
        for entry in pdata.get("recent", []):
            t_str = entry.get("t", "")
            n = entry.get("n", 0)
            if t_str:
                try:
                    t_dt = datetime.fromisoformat(t_str)
                    if start_dt <= t_dt <= end_dt:
                        entries.append((t_dt, n))
                except (ValueError, TypeError):
                    pass

    if not entries:
        # No recent data - create a single summary bucket from totals
        total_processed = 0
        total_time_ms = 0.0
        for pid, pdata in pipelines_data.items():
            if pipeline_id and pid != pipeline_id:
                continue
            total_processed += pdata.get("processed", 0)
            total_time_ms += pdata.get("total_time_ms", 0.0)
        if total_processed > 0:
            avg_lat = round(total_time_ms / total_processed, 1) if total_processed > 0 else None
            return [{
                "t": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:00"),
                "processed": total_processed,
                "failed": 0,
                "throughput": round(total_processed / gran_seconds, 1),
                "avg_latency_ms": avg_lat,
            }]
        return []

    # Bucket entries by granularity
    bucket_map = defaultdict(lambda: {"processed": 0, "failed": 0})
    for t_dt, n in entries:
        if granularity == "minute":
            key = t_dt.strftime("%Y-%m-%dT%H:%M:00")
        elif granularity == "hour":
            key = t_dt.strftime("%Y-%m-%dT%H:00:00")
        else:  # day
            key = t_dt.strftime("%Y-%m-%dT00:00:00")
        bucket_map[key]["processed"] += n

    # Also gather total time for avg latency
    total_time_ms = 0.0
    total_processed = 0
    for pid, pdata in pipelines_data.items():
        if pipeline_id and pid != pipeline_id:
            continue
        total_time_ms += pdata.get("total_time_ms", 0.0)
        total_processed += pdata.get("processed", 0)
    avg_lat = round(total_time_ms / total_processed, 1) if total_processed > 0 else None

    buckets = []
    for t_str in sorted(bucket_map.keys()):
        b = bucket_map[t_str]
        buckets.append({
            "t": t_str,
            "processed": b["processed"],
            "failed": b["failed"],
            "throughput": round(b["processed"] / gran_seconds, 1) if b["processed"] > 0 else 0.0,
            "avg_latency_ms": avg_lat,
        })

    return buckets

@app.get("/api/metrics/timeseries")
async def get_metrics_timeseries(
    granularity: str = "hour",
    start: str | None = None,
    end: str | None = None,
    pipeline_id: str | None = None,
):
    """Get time-series metrics from Azure App Insights (Log Analytics)."""
    now = datetime.utcnow()
    if granularity not in ("minute", "hour", "day"):
        raise HTTPException(status_code=400, detail="granularity must be minute, hour, or day")

    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "")) if end else now
        start_dt = datetime.fromisoformat(start.replace("Z", "")) if start else end_dt - timedelta(hours=24)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid date format. Use ISO 8601.")

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:00")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:00")

    gran_bin = {"minute": "1m", "hour": "1h", "day": "1d"}.get(granularity, "1h")
    gran_seconds = {"minute": 60, "hour": 3600, "day": 86400}.get(granularity, 3600)

    kql = f"""
    customMetrics
    | where name in ('omnivec.documents.embedded', 'omnivec.documents.failed', 'omnivec.embedding.latency')
    | summarize
        processed = sumif(value, name == 'omnivec.documents.embedded'),
        failed = sumif(value, name == 'omnivec.documents.failed'),
        avg_latency = avgif(value, name == 'omnivec.embedding.latency')
        by bin(timestamp, {gran_bin})
    | order by timestamp asc
    """

    rows = await asyncio.to_thread(_run_kql, kql, (start_dt, end_dt))

    if rows is None:
        # Fallback: build buckets from CosmosDB inline metrics
        try:
            buckets = _build_cosmos_metrics_buckets(
                granularity, gran_seconds, start_dt, end_dt, pipeline_id
            )
        except Exception:
            buckets = []
        return {
            "granularity": granularity,
            "start": start_iso,
            "end": end_iso,
            "pipeline_id": pipeline_id,
            "source": "cosmos_inline",
            "buckets": buckets,
        }

    buckets = []
    for row in rows:
        ts = row[0]
        processed = int(row[1] or 0)
        failed = int(row[2] or 0)
        avg_lat = round(row[3], 1) if row[3] else None
        t_str = ts.strftime("%Y-%m-%dT%H:%M:00") if hasattr(ts, 'strftime') else str(ts)[:16] + ":00"
        buckets.append({
            "t": t_str,
            "processed": processed,
            "failed": failed,
            "throughput": round(processed / gran_seconds, 1) if processed > 0 else 0.0,
            "avg_latency_ms": avg_lat,
        })

    return {
        "granularity": granularity,
        "start": start_iso,
        "end": end_iso,
        "pipeline_id": pipeline_id,
        "source": "app_insights",
        "buckets": buckets,
    }



# =============================================================================
# SOURCES
# =============================================================================

@app.get("/api/sources")
def list_sources(request: Request):
    """List all configured sources. Credentials masked for external callers."""
    store = get_store()
    docs = store.list("source")
    internal = getattr(request.state, "internal", False)
    return {"sources": [_source_from_doc(d, mask=not internal) for d in docs]}


@app.get("/api/sources/{source_id}")
def get_source(source_id: str, request: Request):
    """Get a specific source. Credentials masked for external callers."""
    store = get_store()
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    internal = getattr(request.state, "internal", False)
    return _source_from_doc(doc, mask=not internal)


@app.post("/api/sources")
async def create_source(req: CreateSourceRequest):
    """Create a new source."""
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Source name cannot be blank")
    existing = [_source_from_doc(d) for d in store.list("source")]
    if any(s.name.lower() == req.name.strip().lower() for s in existing):
        raise HTTPException(status_code=400, detail=f"Source name '{req.name.strip()}' already exists")
    # Prevent duplicate CosmosDB sources pointing to the same container
    if req.type == SourceType.COSMOSDB:
        for s in existing:
            if s.type == SourceType.COSMOSDB and \
               s.config.get("endpoint", "").rstrip("/").lower() == req.config.get("endpoint", "").rstrip("/").lower() and \
               s.config.get("database", "").lower() == req.config.get("database", "").lower() and \
               s.config.get("container", "").lower() == req.config.get("container", "").lower():
                raise HTTPException(status_code=400, detail=f"A source already exists for this CosmosDB container ('{s.name}'). Multiple sources pointing to the same container cause processing conflicts.")
    source_id = f"src-{str(uuid.uuid4())[:8]}"
    # Strip whitespace from URL fields in config
    clean_config = {k: v.strip() if isinstance(v, str) else v for k, v in req.config.items()}

    # Auto-validate source connectivity
    warnings = []
    # Sources are always created enabled â€” connection test is advisory only
    enabled = True
    if req.type == SourceType.AZURE_BLOB:
        try:
            from connectors.blob_connector import test_blob_connection
            ok, result = await test_blob_connection(clean_config)
            if not ok:
                warnings.append(f"Blob source validation failed: {result}. "
                    "Check account_url, container name, and that the OmniVec managed identity has "
                    "Storage Blob Data Reader role on the storage account.")
        except Exception as e:
            warnings.append(f"Could not connect to blob source: {str(e)}")
    elif req.type == SourceType.COSMOSDB:
        try:
            from connectors.cosmosdb_connector import test_cosmosdb_connection
            ok, result = await test_cosmosdb_connection(clean_config)
            if not ok:
                warnings.append(f"CosmosDB source validation failed: {result}. "
                    "Check endpoint, database, container, and that the OmniVec managed identity has "
                    "Cosmos DB Built-in Data Reader role on the account.")
        except Exception as e:
            warnings.append(f"Could not connect to CosmosDB source: {str(e)}")

    source = Source(
        id=source_id,
        name=req.name.strip(),
        type=req.type,
        config=clean_config,
        triggers=req.triggers,
        schedule=req.schedule,
        enabled=enabled,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    store.upsert(_to_doc(source, "source"))
    result = {"success": True, "source": source}
    if warnings:
        result["warnings"] = warnings
    return result


@app.put("/api/sources/{source_id}")
def update_source(source_id: str, req: CreateSourceRequest):
    """Update a source."""
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Source name cannot be blank")
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    existing = [_source_from_doc(d) for d in store.list("source")]
    if any(s.name.lower() == req.name.strip().lower() and s.id != source_id for s in existing):
        raise HTTPException(status_code=400, detail=f"Source name '{req.name.strip()}' already exists")

    source = _source_from_doc(doc)
    clean_config = {k: v.strip() if isinstance(v, str) else v for k, v in req.config.items()}
    # Preserve stored password if masked value was sent
    for sensitive_key in _SENSITIVE_CONFIG_KEYS:
        if clean_config.get(sensitive_key) == "***":
            clean_config[sensitive_key] = doc.get("config", {}).get(sensitive_key, "")
    source.name = req.name.strip()
    source.type = req.type
    source.config = clean_config
    source.triggers = req.triggers
    source.schedule = req.schedule
    source.enabled = req.enabled
    source.updated_at = datetime.utcnow()

    store.upsert(_to_doc(source, "source"))
    return {"success": True, "source": source}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    """Delete a source."""
    store = get_store()
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")

    # Check if source is used by any pipeline
    pipelines = [_pipeline_from_doc(d) for d in store.list("pipeline")]
    using = [p.name for p in pipelines for ps in p.sources if ps.source_id == source_id]
    if using:
        names = ", ".join(f"'{n}'" for n in using)
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete — source is used by {len(using)} pipeline(s): {names}. "
                   f"Delete or update those pipelines first."
        )

    store.delete(source_id, "source")
    return {"success": True}


@app.post("/api/sources/{source_id}/sync")
async def sync_source(source_id: str, req: SyncSourceRequest):
    """Trigger a sync of the source.

    Activates all pipelines using this source so the controller
    picks them up and creates PENDING jobs for workers.
    """
    store = get_store()
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")

    # Find pipelines using this source and activate them
    activated = 0
    for d in store.list("pipeline"):
        p = _pipeline_from_doc(d)
        for ps in p.sources:
            if ps.source_id == source_id:
                if p.status != PipelineStatus.ACTIVE:
                    p.status = PipelineStatus.ACTIVE
                    p.updated_at = datetime.utcnow()
                    store.upsert(_to_doc(p, "pipeline"))
                activated += 1
                break

    if not activated:
        raise HTTPException(
            status_code=400,
            detail="No pipelines configured for this source"
        )

    return {
        "success": True,
        "message": f"Activated {activated} pipeline(s) â€” controller will enumerate source"
    }


@app.post("/api/sources/{source_id}/test")
async def test_source(source_id: str):
    """Test source connectivity."""
    store = get_store()
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")

    source = _source_from_doc(doc)

    if source.type == SourceType.AZURE_BLOB:
        from connectors.blob_connector import test_blob_connection
        ok, result = await _test_with_timeout(lambda: asyncio.run(test_blob_connection(source.config)))
    elif source.type == SourceType.COSMOSDB:
        from connectors.cosmosdb_connector import test_cosmosdb_connection
        ok, result = await _test_with_timeout(lambda: asyncio.run(test_cosmosdb_connection(source.config)))
    else:
        return {"success": True, "result": {"status": "unknown", "message": "Connector not implemented"}}

    if ok:
        return {"success": True, "result": result}
    return {"success": False, "error": result}


# =============================================================================
# SOURCE CONNECTION TEST (for UI before saving)
# =============================================================================

class TestConnectionRequest(BaseModel):
    type: str
    config: dict
    source_id: Optional[str] = None


@app.post("/api/sources/test-connection")
async def test_source_connection_before_save(req: TestConnectionRequest):
    """Test source connection before saving (used by UI)."""
    # If password is masked, look up real password from stored config
    if req.source_id and req.config.get("password") == "***":
        store = get_store()
        try:
            doc = store.get(req.source_id, partition_key="source")
            stored_pw = doc.get("config", {}).get("password", "")
            if stored_pw:
                req.config["password"] = stored_pw
        except Exception:
            pass
    try:
        if req.type == "azure-blob":
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential

            account_url = (req.config.get("account_url") or "").strip()
            container_name = (req.config.get("container") or "").strip()

            if not account_url or not container_name:
                return {"success": False, "error": "Account URL and Container are required"}

            def _test_blob():
                credential = DefaultAzureCredential(connection_timeout=5)
                client = BlobServiceClient(
                    account_url, credential=credential,
                    connection_timeout=5, read_timeout=8,
                )
                container = client.get_container_client(container_name)
                blob_count = 0
                for blob in container.list_blobs(results_per_page=5):
                    blob_count += 1
                    if blob_count >= 5:
                        break
                return {
                    "success": True,
                    "message": f"Connected successfully. Found {blob_count} blobs in container.",
                    "details": f"Container: {container_name}"
                }

            ok, result = await _test_with_timeout(_test_blob)
            if ok:
                return result
            raise Exception(result)

        elif req.type == "cosmosdb":
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential

            endpoint = req.config.get("endpoint")
            database_name = req.config.get("database")
            container_name = req.config.get("container")

            if not endpoint or not database_name or not container_name:
                return {"success": False, "error": "Endpoint, Database, and Container are required"}

            def _test_cosmos():
                credential = DefaultAzureCredential(connection_timeout=5)
                client = CosmosClient(
                    endpoint, credential=credential,
                    connection_timeout=5, request_timeout=8,
                )
                database = client.get_database_client(database_name)
                container = database.get_container_client(container_name)
                props = container.read()
                return {
                    "success": True,
                    "message": "Connected successfully to CosmosDB.",
                    "details": f"Database: {database_name}, Container: {container_name}"
                }

            ok, result = await _test_with_timeout(_test_cosmos)
            if ok:
                return result
            raise Exception(result)

        elif req.type == "postgresql":
            from health_checker import _connect_pg
            table = req.config.get("table", "")
            conn = await _connect_pg(req.config)
            try:
                row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"') if table else 0
                return {
                    "success": True,
                    "message": f"Connected successfully to PostgreSQL. {row_count} rows in '{table}'.",
                    "details": f"Table: {table}"
                }
            finally:
                await conn.close()

        elif req.type == "mssql":
            conn_str = _build_mssql_odbc_conn_str(req.config)
            server = req.config.get("server", req.config.get("host", ""))
            database_name = req.config.get("database", "")
            table = req.config.get("table", "")

            import pyodbc
            def _test_mssql():
                conn = pyodbc.connect(conn_str, timeout=10)
                try:
                    schema = req.config.get("schema_name", req.config.get("schema", "dbo"))
                    cursor = conn.cursor()
                    cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
                    row_count = cursor.fetchone()[0]
                    return {
                        "success": True,
                        "message": f"Connected successfully to MS SQL. {row_count} rows in '{schema}.{table}'.",
                        "details": f"Server: {server}, Database: {database_name}"
                    }
                finally:
                    conn.close()

            ok, result = await _test_with_timeout(_test_mssql)
            if ok:
                return result
            raise Exception(result)

        else:
            return {"success": False, "error": f"Unsupported source type: {req.type}"}

    except Exception as e:
        error_msg = str(e)
        # Simplify common error messages
        if "AuthorizationPermissionMismatch" in error_msg or "authorization" in error_msg.lower():
            error_msg = "Access denied. The managed identity does not have permission to access this resource."
        elif "ResourceNotFound" in error_msg:
            error_msg = "Resource not found. Check the account URL, database, or container name."
        elif "InvalidAuthenticationInfo" in error_msg:
            error_msg = "Authentication failed. Check your credentials or managed identity configuration."
        elif "Connection refused" in error_msg or "[Errno 111]" in error_msg:
            error_msg = "Connection refused. Check that the host and port are correct, the database server is running, and the firewall allows connections from this service."
        elif "no PostgreSQL user name" in error_msg:
            error_msg = "No PostgreSQL username provided. Please enter your database username in the Authentication tab."
        elif "password authentication failed" in error_msg:
            error_msg = "Password authentication failed. Check that the username and password are correct."
        elif "does not exist" in error_msg and "database" in error_msg.lower():
            error_msg = f"Database not found. Verify the database name is correct. Original error: {error_msg}"
        elif "could not translate host name" in error_msg or "Name or service not known" in error_msg:
            error_msg = "Cannot resolve hostname. Check that the host address is correct."
        elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            error_msg = "Connection timed out. Check that the host is reachable and the firewall allows connections."

        return {"success": False, "error": error_msg}


# =============================================================================
# SOURCE DEPLOYMENTS (per-source enumerator + worker)
# =============================================================================

@app.get("/api/sources/{source_id}/deployments")
async def get_source_deployments(source_id: str):
    """Legacy endpoint â€” source deployments are no longer used. .NET CFP handles all sources."""
    return {"deployments": [], "message": "Source deployments removed. Processing handled by .NET ChangeFeed Processor."}


@app.post("/api/sources/{source_id}/deployments")
async def create_source_deployments_legacy(source_id: str):
    """Legacy endpoint â€” removed. .NET CFP handles all source processing."""
    raise HTTPException(status_code=410, detail="Source deployments removed. Processing handled by .NET ChangeFeed Processor.")


@app.delete("/api/sources/{source_id}/deployments")
async def delete_source_deployments_legacy(source_id: str):
    """Legacy endpoint â€” removed. .NET CFP handles all source processing."""
    raise HTTPException(status_code=410, detail="Source deployments removed. Processing handled by .NET ChangeFeed Processor.")


@app.post("/api/sources/{source_id}/deployments")
async def create_source_deployments(source_id: str):
    """Legacy â€” source deployments removed. .NET CFP handles all source processing."""
    raise HTTPException(status_code=410, detail="Source deployments removed. Processing handled by .NET ChangeFeed Processor.")


@app.delete("/api/sources/{source_id}/deployments")
async def delete_source_deployments(source_id: str):
    """Legacy â€” source deployments removed."""
    raise HTTPException(status_code=410, detail="Source deployments removed. Processing handled by .NET ChangeFeed Processor.")


# NOTE: ~250 lines of legacy K8s deployment code removed here.
# All source processing is now handled by .NET ChangeFeed Processor + .NET Worker.
# See git history for the removed create_source_deployments and delete_source_deployments code.


# =============================================================================
# DESTINATIONS
# =============================================================================

@app.get("/api/destinations")
def list_destinations(request: Request):
    """List all configured destinations. Credentials masked for external callers."""
    store = get_store()
    docs = store.list("destination")
    internal = getattr(request.state, "internal", False)
    return {"destinations": [_destination_from_doc(d, mask=not internal) for d in docs]}


@app.get("/api/destinations/{dest_id}")
def get_destination(dest_id: str, request: Request):
    """Get a specific destination. Credentials masked for external callers."""
    store = get_store()
    doc = store.get(dest_id, "destination")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Destination '{dest_id}' not found")
    internal = getattr(request.state, "internal", False)
    return _destination_from_doc(doc, mask=not internal)


@app.post("/api/destinations")
async def create_destination(req: CreateDestinationRequest):
    """Create a new destination."""
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Destination name cannot be blank")
    existing = [_destination_from_doc(d) for d in store.list("destination")]
    if any(d.name.lower() == req.name.strip().lower() for d in existing):
        raise HTTPException(status_code=400, detail=f"Destination name '{req.name.strip()}' already exists")
    # Prevent duplicate CosmosDB destinations pointing to the same container
    if req.type == "cosmosdb-vector":
        for d in existing:
            if d.type == "cosmosdb-vector" and \
               d.config.get("endpoint", "").rstrip("/").lower() == req.config.get("endpoint", "").rstrip("/").lower() and \
               d.config.get("database", "").lower() == req.config.get("database", "").lower() and \
               d.config.get("container", "").lower() == req.config.get("container", "").lower():
                raise HTTPException(status_code=400, detail=f"A destination already exists for this CosmosDB container ('{d.name}'). Multiple destinations pointing to the same container cause processing conflicts.")

    # Auto-probe CosmosDB container for partition key, vector field, and validate
    config = dict(req.config)
    warnings = []
    enabled = True
    if req.type == "cosmosdb-vector":
        try:
            from connectors.cosmosdb_vector_connector import test_vector_connection
            probe_result = await test_vector_connection(config)
            # Auto-set partition key
            if "partition_key_path" not in config and probe_result.get("partition_key_path"):
                config["partition_key_path"] = probe_result["partition_key_path"]
            # Auto-set vector field
            if "vector_field" not in config and probe_result.get("vector_field"):
                config["vector_field"] = probe_result["vector_field"]
            # Store all vector indexes from probe
            if probe_result.get("vector_indexes"):
                config["vector_indexes"] = probe_result["vector_indexes"]
            # Check vector embedding policy
            if not probe_result.get("has_vector_policy"):
                enabled = False
                warnings.append("No vector embedding policy found on container. "
                    "Please enable vector search on your CosmosDB account and configure "
                    "a vector embedding policy on the container before using this destination. "
                    "See: https://aka.ms/cosmos-vector-search")
            # Check vector indexes
            if not probe_result.get("vector_indexes"):
                warnings.append("No vector indexes found. Consider adding a vector index "
                    "for better search performance.")
        except Exception as e:
            enabled = False
            warnings.append(f"Could not connect to destination: {str(e)}. "
                "Check endpoint, database, container, and permissions.")

    # Auto-probe pgvector table for vector columns
    elif req.type == "pgvector":
        try:
            from connectors.postgres_connector import test_destination_connection as _test_pg
            probe_result = await _test_pg(config)
            if probe_result.get("vector_indexes"):
                config["vector_indexes"] = probe_result["vector_indexes"]
            if probe_result.get("vector_field"):
                config["vector_field"] = probe_result["vector_field"]
            if not probe_result.get("has_vector_policy"):
                warnings.append("No vector columns found in table. "
                    "Ensure the table has columns of type vector(N).")
        except Exception as e:
            warnings.append(f"Could not probe pgvector table: {str(e)}. "
                "Vector column discovery skipped.")

    # Auto-probe MSSQL table for vector columns
    elif req.type == "mssql":
        try:
            import pyodbc
            conn_str = _build_mssql_odbc_conn_str(config)
            mssql_conn = pyodbc.connect(conn_str, timeout=10)
            try:
                table = config.get("table", "vectors")
                schema = config.get("schema_name", config.get("schema", "dbo"))
                cursor = mssql_conn.cursor()
                vi = _discover_mssql_vector_columns(cursor, table, schema, config)
                if vi:
                    config["vector_indexes"] = vi
            finally:
                mssql_conn.close()
        except Exception as e:
            warnings.append(f"Could not probe MSSQL table: {str(e)}. "
                "Vector column discovery skipped.")

    dest_id = f"dst-{str(uuid.uuid4())[:8]}"
    destination = Destination(
        id=dest_id,
        name=req.name,
        type=req.type,
        config=config,
        enabled=enabled,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    store.upsert(_to_doc(destination, "destination"))
    result = {"success": True, "destination": destination}
    if warnings:
        result["warnings"] = warnings
    return result


@app.put("/api/destinations/{dest_id}")
def update_destination(dest_id: str, req: CreateDestinationRequest):
    """Update a destination."""
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Destination name cannot be blank")
    doc = store.get(dest_id, "destination")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Destination '{dest_id}' not found")
    existing = [_destination_from_doc(d) for d in store.list("destination")]
    if any(d.name.lower() == req.name.strip().lower() and d.id != dest_id for d in existing):
        raise HTTPException(status_code=400, detail=f"Destination name '{req.name.strip()}' already exists")

    destination = _destination_from_doc(doc)
    clean_config = {k: v.strip() if isinstance(v, str) else v for k, v in req.config.items()}
    # Preserve stored password if masked value was sent
    for sensitive_key in _SENSITIVE_CONFIG_KEYS:
        if clean_config.get(sensitive_key) == "***":
            clean_config[sensitive_key] = doc.get("config", {}).get(sensitive_key, "")
    destination.name = req.name
    destination.type = req.type
    destination.config = clean_config
    destination.enabled = req.enabled
    destination.updated_at = datetime.utcnow()

    store.upsert(_to_doc(destination, "destination"))
    return {"success": True, "destination": destination}


@app.delete("/api/destinations/{dest_id}")
def delete_destination(dest_id: str):
    """Delete a destination."""
    store = get_store()
    doc = store.get(dest_id, "destination")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Destination '{dest_id}' not found")

    # Check if destination is used by any pipeline
    pipelines = [_pipeline_from_doc(d) for d in store.list("pipeline")]
    using = [p.name for p in pipelines if p.destination_id == dest_id]
    if using:
        names = ", ".join(f"'{n}'" for n in using)
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete — destination is used by {len(using)} pipeline(s): {names}. "
                   f"Delete or update those pipelines first."
        )

    store.delete(dest_id, "destination")
    return {"success": True}


@app.post("/api/destinations/{dest_id}/test")
async def test_destination(dest_id: str):
    """Test destination connectivity."""
    store = get_store()
    doc = store.get(dest_id, "destination")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Destination '{dest_id}' not found")

    destination = _destination_from_doc(doc)

    if destination.type == "pgvector":
        from connectors.postgres_connector import test_destination_connection as _test_pg
        ok, result = await _test_with_timeout(lambda: asyncio.run(_test_pg(destination.config)))
    elif destination.type == "mssql":
        def _test_mssql_dest():
            import pyodbc
            conn_str = _build_mssql_odbc_conn_str(destination.config)
            conn = pyodbc.connect(conn_str, timeout=10)
            try:
                table = destination.config.get("table", "vectors")
                schema = destination.config.get("schema_name", destination.config.get("schema", "dbo"))
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
                row_count = cursor.fetchone()[0]
                vi = _discover_mssql_vector_columns(cursor, table, schema, destination.config)
                return {
                    "status": "connected",
                    "table": f"{schema}.{table}",
                    "row_count": row_count,
                    "vector_indexes": vi,
                    "has_vector_policy": len(vi) > 0,
                }
            finally:
                conn.close()
        ok, result = await _test_with_timeout(_test_mssql_dest)
    else:
        from connectors.cosmosdb_vector_connector import test_vector_connection
        ok, result = await _test_with_timeout(lambda: asyncio.run(test_vector_connection(destination.config)))

    if ok:
        # Persist vector_indexes back into destination config so pipeline creation can use them
        vi = result.get("vector_indexes")
        if vi is not None:
            config = dict(destination.config)
            config["vector_indexes"] = vi
            destination.config = config
            store.upsert(_to_doc(destination, "destination"))
        return {"success": True, "result": result}
    return {"success": False, "error": result}


# =============================================================================
# DESTINATION CONNECTION TEST (for UI before saving)
# =============================================================================

class TestDestConnectionRequest(BaseModel):
    type: str
    config: dict
    destination_id: Optional[str] = None


@app.post("/api/destinations/test-connection")
async def test_destination_connection_before_save(req: TestDestConnectionRequest):
    """Test destination connection before saving (used by UI)."""
    # If password is masked, look up real password from stored config
    if req.destination_id and req.config.get("password") == "***":
        store = get_store()
        try:
            doc = store.get(req.destination_id, partition_key="destination")
            stored_pw = doc.get("config", {}).get("password", "")
            if stored_pw:
                req.config["password"] = stored_pw
        except Exception:
            pass
    try:
        if req.type == "cosmosdb-vector":
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential

            endpoint = req.config.get("endpoint")
            database_name = req.config.get("database")
            container_name = req.config.get("container")

            if not endpoint or not database_name or not container_name:
                return {"success": False, "error": "Endpoint, Database, and Container are required"}

            def _test_dest_cosmos():
                credential = DefaultAzureCredential(connection_timeout=5)
                client = CosmosClient(
                    endpoint, credential=credential,
                    connection_timeout=5, request_timeout=8,
                )
                database = client.get_database_client(database_name)
                container = database.get_container_client(container_name)
                props = container.read()

                indexing_policy = props.get("indexingPolicy", {})
                vector_indexes = indexing_policy.get("vectorIndexes", [])
                vector_embedding_policy = props.get("vectorEmbeddingPolicy", {})
                vector_embeddings = vector_embedding_policy.get("vectorEmbeddings", [])
                has_vector_index = len(vector_indexes) > 0

                structured_indexes = []
                for vi in vector_indexes:
                    vi_path = vi.get("path", "")
                    vi_type = vi.get("type", "")
                    embedding_info = {}
                    for ve in vector_embeddings:
                        if ve.get("path") == vi_path:
                            embedding_info = ve
                            break
                    structured_indexes.append({
                        "path": vi_path,
                        "indexType": vi_type,
                        "dimensions": embedding_info.get("dimensions"),
                        "dataType": embedding_info.get("dataType"),
                        "distanceFunction": embedding_info.get("distanceFunction"),
                        "quantizationByteSize": vi.get("quantizationByteSize"),
                    })

                return {
                    "success": True,
                    "message": f"Connected successfully to CosmosDB.{' Vector indexing configured.' if has_vector_index else ' Warning: No vector index found.'}",
                    "details": f"Database: {database_name}, Container: {container_name}",
                    "vector_indexes": structured_indexes
                }

            ok, result = await _test_with_timeout(_test_dest_cosmos)
            if ok:
                return result
            raise Exception(result)

        elif req.type == "pinecone":
            # Pinecone requires API key
            api_key = req.config.get("api_key")
            index_name = req.config.get("index")

            if not api_key or not index_name:
                return {"success": False, "error": "API Key and Index name are required"}

            # For now, just validate config is present
            return {
                "success": True,
                "message": "Configuration validated. Pinecone connection will be tested on first write.",
                "details": f"Index: {index_name}"
            }

        elif req.type == "pgvector":
            from health_checker import _connect_pg
            from connectors.postgres_connector import _discover_vector_columns
            table = req.config.get("table", "vectors")
            conn = await _connect_pg(req.config)
            try:
                row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')

                ext = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")

                details = []
                details.append(f"Table: {table}, {row_count} rows")
                if ext:
                    details.append(f"pgvector v{ext}")

                # Discover all vector columns from table schema
                vector_indexes = await _discover_vector_columns(conn, table)

                if vector_indexes:
                    details.append(f"{len(vector_indexes)} vector column(s) found")
                else:
                    details.append("No vector columns found")

                return {
                    "success": True,
                    "message": f"Connected to PostgreSQL. {'; '.join(details)}",
                    "details": "; ".join(details),
                    "vector_indexes": vector_indexes
                }
            finally:
                await conn.close()

        elif req.type == "mssql":
            try:
                import pyodbc
            except ImportError:
                return {"success": False, "error": "pyodbc not installed. MSSQL destination tests handled by changefeed connector"}

            conn_str = _build_mssql_odbc_conn_str(req.config)

            conn = pyodbc.connect(conn_str, timeout=10)
            try:
                table = req.config.get("table", "vectors")
                schema = req.config.get("schema_name", req.config.get("schema", "dbo"))
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
                row_count = cursor.fetchone()[0]

                vector_indexes = _discover_mssql_vector_columns(cursor, table, schema, req.config)

                vec_msg = ""
                if vector_indexes:
                    vec_msg = f" {len(vector_indexes)} vector column(s) found."

                return {
                    "success": True,
                    "message": f"Connected to MS SQL. {row_count} rows in '{schema}.{table}'.{vec_msg}",
                    "details": f"Schema: {schema}, Table: {table}",
                    "vector_indexes": vector_indexes,
                }
            finally:
                conn.close()

        else:
            return {"success": False, "error": f"Unsupported destination type: {req.type}"}

    except Exception as e:
        error_msg = str(e)
        # Simplify common error messages
        if "Forbidden" in error_msg or "authorization" in error_msg.lower():
            error_msg = "Access denied. The managed identity does not have the required CosmosDB data contributor role."
        elif "ResourceNotFound" in error_msg or "NotFound" in error_msg:
            error_msg = "Resource not found. Check the endpoint, database, or container name."
        elif "InvalidAuthenticationInfo" in error_msg:
            error_msg = "Authentication failed. Check your credentials or managed identity configuration."
        elif "Connection refused" in error_msg or "[Errno 111]" in error_msg:
            error_msg = "Connection refused. Check that the host and port are correct, the database server is running, and the firewall allows connections from this service."
        elif "no PostgreSQL user name" in error_msg:
            error_msg = "No PostgreSQL username provided. Please enter your database username in the Authentication tab."
        elif "password authentication failed" in error_msg:
            error_msg = "Password authentication failed. Check that the username and password are correct."
        elif "does not exist" in error_msg and "database" in error_msg.lower():
            error_msg = f"Database not found. Verify the database name is correct. Original error: {error_msg}"
        elif "could not translate host name" in error_msg or "Name or service not known" in error_msg:
            error_msg = "Cannot resolve hostname. Check that the host address is correct."
        elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            error_msg = "Connection timed out. Check that the host is reachable and the firewall allows connections."

        return {"success": False, "error": error_msg}


# =============================================================================
# PIPELINES
# =============================================================================

@app.get("/api/pipelines")
def list_pipelines():
    """List all pipelines."""
    store = get_store()
    result = []
    for d in store.list("pipeline"):
        p = _pipeline_from_doc(d)
        stats = get_pipeline_stats(p.id)
        result.append({
            **p.model_dump(),
            "cfp_generation": _cfp_generation(p.reset_at),
            "stats": stats.model_dump()
        })
    return {"pipelines": result}


@app.get("/api/pipelines/{pipeline_id}")
def get_pipeline(pipeline_id: str):
    """Get a specific pipeline."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    p = _pipeline_from_doc(doc)
    stats = get_pipeline_stats(pipeline_id)

    return {
        **p.model_dump(),
        "cfp_generation": _cfp_generation(p.reset_at),
        "stats": stats.model_dump()
    }


async def _validate_docgrok_ref(docgrok_ref: str) -> str:
    """Validate a DocGrok model ID or transform pipeline name.
    Returns the reference as-is if valid. Only embedding models allowed for pipelines."""
    if docgrok_ref.startswith("mdl-ext-") or docgrok_ref.startswith("mdl-native-"):
        # Check model_category in CosmosDB â€” chat models cannot be used in pipelines
        store = get_store()
        try:
            doc = store.get(docgrok_ref, "docgrok_model")
            if doc and doc.get("model_category") == "chat":
                raise HTTPException(status_code=400, detail=f"Model '{docgrok_ref}' is a chat model and cannot be used in embedding pipelines. Only embedding models are allowed.")
        except HTTPException:
            raise
        except Exception:
            pass
        # Validate model exists via DocGrok /models endpoint
        try:
            resp = await http_client.get(f"{DOCGROK_URL}/models")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                if not any(m.get("id") == docgrok_ref for m in models):
                    raise HTTPException(status_code=400, detail=f"Model '{docgrok_ref}' not found in DocGrok")
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Cannot connect to DocGrok: {str(e)}")
        return docgrok_ref
    elif docgrok_ref.startswith("trp-"):
        # Transform pipeline (future)
        return docgrok_ref
    elif docgrok_ref in ("mock-embedding", "mock-1536"):
        # Mock pipelines for testing
        return docgrok_ref
    else:
        raise HTTPException(status_code=400, detail=f"Invalid DocGrok reference '{docgrok_ref}'. Must be a model ID (mdl-ext-*, mdl-native-*) or transform pipeline (trp-*)")


@app.post("/api/pipelines")
async def create_pipeline(req: CreatePipelineRequest):
    """Create a new pipeline."""
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Pipeline name cannot be blank")
    existing_pipelines = [_pipeline_from_doc(d) for d in store.list("pipeline")]
    if any(p.name.lower() == req.name.strip().lower() for p in existing_pipelines):
        raise HTTPException(status_code=400, detail=f"Pipeline name '{req.name.strip()}' already exists")

    # Validate sources exist
    for ps in req.sources:
        if not store.get(ps.source_id, "source"):
            raise HTTPException(
                status_code=400,
                detail=f"Source '{ps.source_id}' not found"
            )

    # Validate destination exists
    dest_doc = store.get(req.destination_id, "destination")
    if not dest_doc:
        raise HTTPException(
            status_code=400,
            detail=f"Destination '{req.destination_id}' not found"
        )

    # Validate vector_index_path exists in destination's vector policies
    dest_config = dest_doc.get("config", {})
    vector_indexes = dest_config.get("vector_indexes", [])
    if vector_indexes:
        valid_paths = [vi.get("path", "").lstrip("/") for vi in vector_indexes]
        if req.vector_index_path.lstrip("/") not in valid_paths:
            raise HTTPException(
                status_code=400,
                detail=f"Vector index path '/{req.vector_index_path}' not found in destination. Available: {[f'/{p}' for p in valid_paths]}"
            )

    # Validate docgrok_pipeline (must be a valid model ID or transform pipeline)
    resolved_pipeline = await _validate_docgrok_ref(req.docgrok_pipeline)

    # Validate content strategy and chunk config
    content_strategy = req.content_strategy if req.content_strategy in ("truncate", "chunk") else "truncate"
    chunk_config = None
    if content_strategy == "chunk":
        from models import ChunkConfig
        cc = req.chunk_config or {}
        if cc.get("chunk_size", 1000) < 100:
            raise HTTPException(status_code=400, detail="chunk_size must be >= 100")
        if cc.get("chunk_overlap", 0) >= cc.get("chunk_size", 1000):
            raise HTTPException(status_code=400, detail="chunk_overlap must be less than chunk_size")
        chunk_config = ChunkConfig(**cc)

    # Pipeline always starts PAUSED â€” user must explicitly resume/run to activate
    initial_status = PipelineStatus.PAUSED

    pipeline_id = f"pip-{str(uuid.uuid4())[:8]}"
    pipeline = Pipeline(
        id=pipeline_id,
        name=req.name,
        description=req.description,
        sources=req.sources,
        docgrok_pipeline=resolved_pipeline,
        destination_id=req.destination_id,
        vector_index_path=req.vector_index_path,
        status=initial_status,
        process_existing=req.process_existing,
        metadata_mapping=req.metadata_mapping,
        processing_mode=req.processing_mode,
        content_strategy=content_strategy,
        chunk_config=chunk_config,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    store.upsert(_to_doc(pipeline, "pipeline"))

    return {"success": True, "pipeline": pipeline}


@app.put("/api/pipelines/{pipeline_id}")
async def update_pipeline(pipeline_id: str, req: CreatePipelineRequest):
    """Update a pipeline."""
    store = get_store()
    doc = await asyncio.to_thread(store.get, pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    resolved_pipeline = await _validate_docgrok_ref(req.docgrok_pipeline)

    # Validate vector_index_path against destination if provided
    dest_doc = await asyncio.to_thread(store.get, req.destination_id, "destination")
    if dest_doc:
        dest_config = dest_doc.get("config", {})
        vector_indexes = dest_config.get("vector_indexes", [])
        if vector_indexes:
            valid_paths = [vi.get("path", "").lstrip("/") for vi in vector_indexes]
            if req.vector_index_path.lstrip("/") not in valid_paths:
                raise HTTPException(
                    status_code=400,
                    detail=f"Vector index path '/{req.vector_index_path}' not found in destination. Available: {[f'/{p}' for p in valid_paths]}"
                )

    pipeline = _pipeline_from_doc(doc)

    # Sources, destination, and vector_index_path are immutable after creation
    if req.sources and [s.dict() for s in req.sources] != [s.dict() if hasattr(s, 'dict') else s for s in pipeline.sources]:
        raise HTTPException(status_code=400, detail="Sources cannot be changed after pipeline creation. Create a new pipeline instead.")
    if req.destination_id != pipeline.destination_id:
        raise HTTPException(status_code=400, detail="Destination cannot be changed after pipeline creation. Create a new pipeline instead.")
    if req.vector_index_path.lstrip("/") != pipeline.vector_index_path.lstrip("/"):
        raise HTTPException(status_code=400, detail="Vector index path cannot be changed after pipeline creation. Create a new pipeline instead.")

    pipeline.name = req.name
    pipeline.description = req.description
    pipeline.docgrok_pipeline = resolved_pipeline
    pipeline.metadata_mapping = req.metadata_mapping
    pipeline.processing_mode = req.processing_mode
    pipeline.content_strategy = req.content_strategy if req.content_strategy in ("truncate", "chunk") else pipeline.content_strategy
    if req.chunk_config and pipeline.content_strategy == "chunk":
        from models import ChunkConfig
        pipeline.chunk_config = ChunkConfig(**req.chunk_config)
    pipeline.updated_at = datetime.utcnow()

    await asyncio.to_thread(store.upsert, _to_doc(pipeline, "pipeline"))
    return {"success": True, "pipeline": pipeline}


@app.delete("/api/pipelines/{pipeline_id}")
def delete_pipeline(pipeline_id: str):
    """Delete a pipeline."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    store.delete(pipeline_id, "pipeline")
    return {"success": True}


@app.post("/api/pipelines/{pipeline_id}/pause")
def pause_pipeline(pipeline_id: str):
    """Pause a pipeline."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    pipeline = _pipeline_from_doc(doc)
    pipeline.status = PipelineStatus.PAUSED
    pipeline.updated_at = datetime.utcnow()
    store.upsert(_to_doc(pipeline, "pipeline"))
    return {"success": True}


@app.post("/api/pipelines/{pipeline_id}/resume")
def resume_pipeline(pipeline_id: str):
    """Resume a pipeline."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    pipeline = _pipeline_from_doc(doc)
    pipeline.status = PipelineStatus.ACTIVE
    pipeline.updated_at = datetime.utcnow()
    store.upsert(_to_doc(pipeline, "pipeline"))
    return {"success": True}


@app.post("/api/pipelines/{pipeline_id}/processing-mode/{mode}")
def set_processing_mode(pipeline_id: str, mode: str):
    """Set pipeline processing mode: 'queue' or 'inline'."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")
    if mode not in ("queue", "inline"):
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'. Must be 'queue' or 'inline'.")
    pipeline = _pipeline_from_doc(doc)
    pipeline.processing_mode = mode
    pipeline.updated_at = datetime.utcnow()
    store.upsert(_to_doc(pipeline, "pipeline"))
    return {"success": True, "processing_mode": mode}


@app.post("/api/pipelines/{pipeline_id}/run")
def run_pipeline(pipeline_id: str):
    """Activate a pipeline for continuous processing.

    Sets pipeline status to ACTIVE. The controller picks up active pipelines
    and starts creating PENDING jobs; workers process them.
    """
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    pipeline = _pipeline_from_doc(doc)
    pipeline.status = PipelineStatus.ACTIVE
    pipeline.updated_at = datetime.utcnow()
    store.upsert(_to_doc(pipeline, "pipeline"))

    return {"success": True, "message": "Pipeline activated â€” controller will begin processing"}


@app.post("/api/pipelines/{pipeline_id}/reset")
def reset_pipeline(pipeline_id: str):
    """Reset a pipeline: pause it first, delete all jobs, then allow resume.

    Sets reset_at which the .NET CFP service detects â€” it will automatically
    delete its lease container and restart the change feed from the beginning.
    """
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    pipeline = _pipeline_from_doc(doc)

    # Force pause before reset to prevent race with active changefeed
    if pipeline.status == PipelineStatus.ACTIVE:
        pipeline.status = PipelineStatus.PAUSED
        pipeline.updated_at = datetime.utcnow()
        store.upsert(_to_doc(pipeline, "pipeline"))
        logger.info("Pipeline %s paused before reset", pipeline_id)

    # Delete all jobs for this pipeline (skip for inline mode â€” no jobs created)
    deleted = 0
    if pipeline.processing_mode != "inline":
        job_ids = store.query(
            "SELECT c.id FROM c WHERE c.doc_type = 'job' AND c.pipeline_id = @pid",
            [{"name": "@pid", "value": pipeline_id}],
            partition_key="job",
        )
        for j in job_ids:
            try:
                store.delete(j["id"], "job")
                deleted += 1
            except Exception:
                pass

    # Reset pipeline metrics
    metrics_doc = store.get("global", "metrics")
    if metrics_doc and pipeline_id in metrics_doc.get("pipelines", {}):
        pip_metrics = metrics_doc["pipelines"][pipeline_id]
        # Subtract pipeline counts from global totals
        metrics_doc["events_processed"] = max(0, metrics_doc.get("events_processed", 0) - pip_metrics.get("processed", 0))
        metrics_doc["events_failed"] = max(0, metrics_doc.get("events_failed", 0) - pip_metrics.get("failed", 0))
        metrics_doc["pipelines"][pipeline_id] = {"processed": 0, "failed": 0, "total_time_ms": 0.0, "recent": [], "seen_batches": []}
        store.upsert(metrics_doc)

    # If pipeline uses chunking, delete all chunk vector documents from destination
    chunks_deleted = 0
    if getattr(pipeline, 'content_strategy', 'truncate') == 'chunk':
        try:
            dest_doc = store.get(pipeline.destination_id, "destination")
            if dest_doc:
                destination = _destination_from_doc(dest_doc)
                from connectors.cosmosdb_vector_connector import delete_chunks_by_prefix
                import asyncio
                chunk_prefix = f"{pipeline_id}-"
                chunks_deleted = asyncio.get_event_loop().run_until_complete(
                    delete_chunks_by_prefix(destination.config, chunk_prefix)
                )
                logger.info("Deleted %d chunk documents for pipeline %s", chunks_deleted, pipeline_id)
        except Exception as e:
            logger.warning("Failed to clean up chunks for pipeline %s: %s", pipeline_id, e)

    # Set reset_at â€” the .NET CFP service watches this and will delete its
    # lease container + restart the change feed from the beginning
    # Generate new generation hash from context (source+dest+model+timestamp)
    import hashlib
    reset_ts = datetime.utcnow()
    source_ids = "+".join(sorted([s.source_id for s in pipeline.sources]))
    gen_input = f"{source_ids}|{pipeline.destination_id}|{pipeline.docgrok_pipeline}|{reset_ts.isoformat()}"
    pipeline.generation = hashlib.sha256(gen_input.encode()).hexdigest()[:12]
    pipeline.reset_at = reset_ts
    pipeline.updated_at = reset_ts
    store.upsert(_to_doc(pipeline, "pipeline"))

    return {"success": True, "deleted_jobs": deleted, "chunks_deleted": chunks_deleted, "message": f"Pipeline reset â€” {deleted} jobs deleted, {chunks_deleted} chunks cleaned, CFP will restart"}


@app.post("/api/pipelines/{pipeline_id}/metrics/inline")
def report_inline_metrics(pipeline_id: str, payload: dict):
    """Report inline processing metrics from CFP (continuous, cumulative).
    Payload: {processed: N, failed: M, processing_time_ms: T, batch_key: str}
    batch_key is used for deduplication â€” duplicate reports from lease rebalancing are ignored."""
    store = get_store()
    doc = store.get("global", "metrics")
    if not doc:
        doc = {"id": "global", "doc_type": "metrics", "events_processed": 0, "events_failed": 0, "pipelines": {}}

    processed = int(payload.get("processed", 0))
    failed = int(payload.get("failed", 0))
    processing_time_ms = float(payload.get("processing_time_ms", 0))
    batch_key = payload.get("batch_key", "")

    if pipeline_id not in doc.get("pipelines", {}):
        doc.setdefault("pipelines", {})[pipeline_id] = {"processed": 0, "failed": 0, "total_time_ms": 0.0, "recent": [], "seen_batches": []}

    pip = doc["pipelines"][pipeline_id]

    # Deduplicate: skip if we've already recorded this batch
    if batch_key:
        seen = pip.get("seen_batches", [])
        if batch_key in seen:
            return {"ok": True, "dedup": True}
        seen.append(batch_key)
        # Keep last 500 batch keys to bound memory
        if len(seen) > 500:
            seen = seen[-500:]
        pip["seen_batches"] = seen

    doc["events_processed"] = doc.get("events_processed", 0) + processed
    doc["events_failed"] = doc.get("events_failed", 0) + failed

    pip["processed"] = pip.get("processed", 0) + processed
    pip["failed"] = pip.get("failed", 0) + failed
    pip["total_time_ms"] = pip.get("total_time_ms", 0.0) + processing_time_ms

    # Keep a rolling window of recent reports for throughput calculation
    now = datetime.utcnow().isoformat()
    recent = pip.get("recent", [])
    recent.append({"t": now, "n": processed})
    # Keep last 60 entries max
    if len(recent) > 60:
        recent = recent[-60:]
    pip["recent"] = recent

    store.upsert(doc)

    return {"ok": True}


# =============================================================================
# JOBS
# =============================================================================

@app.get("/api/jobs")
def list_jobs(
    pipeline_id: Optional[str] = None,
    status: Optional[JobStatus] = None,
    limit: int = 100
):
    """List jobs with optional filters using server-side query."""
    store = get_store()

    # Build parameterized query with filters
    conditions = ["c.doc_type = 'job'"]
    params = []
    if pipeline_id:
        conditions.append("c.pipeline_id = @pid")
        params.append({"name": "@pid", "value": pipeline_id})
    if status:
        conditions.append("c.status = @status")
        params.append({"name": "@status", "value": status.value})

    limit = max(1, min(limit, 1000))  # Cap between 1-1000
    query = f"SELECT TOP {limit} * FROM c WHERE {' AND '.join(conditions)} ORDER BY c.created_at DESC"
    docs = store.query(query, params, partition_key="job")
    jobs = [_job_from_doc(d) for d in docs]

    return {"jobs": jobs}


class BulkJobEntry(BaseModel):
    pipeline_id: str
    source_id: str
    source_ref: str
    metadata: Dict[str, Any] = {}


class BulkJobRequest(BaseModel):
    jobs: List[BulkJobEntry] = []


@app.post("/api/jobs/bulk")
def create_jobs_bulk(body: BulkJobRequest):
    """Create multiple PENDING jobs in one call. Used by the .NET Change Feed Processor.

    Idempotent: skips jobs where (pipeline_id, source_id, source_ref) already exists
    with a non-terminal status.
    """
    entries = body.jobs
    if not entries:
        return {"created": 0, "skipped": 0}

    store = get_store()
    created = 0
    skipped = 0

    for entry in entries:
        pipeline_id = entry.pipeline_id
        source_id = entry.source_id
        source_ref = entry.source_ref
        metadata = entry.metadata

        if not pipeline_id or not source_id or not source_ref:
            skipped += 1
            continue

        # Check for existing job with same (pipeline, source, ref) that isn't terminal
        existing = store.query(
            "SELECT c.id FROM c WHERE c.doc_type = 'job' "
            "AND c.pipeline_id = @pid AND c.source_id = @sid AND c.source_ref = @ref "
            "AND c.status NOT IN ('failed', 'cancelled')",
            [
                {"name": "@pid", "value": pipeline_id},
                {"name": "@sid", "value": source_id},
                {"name": "@ref", "value": source_ref},
            ],
            partition_key="job",
        )
        if existing:
            skipped += 1
            continue

        job = Job(
            id=f"job-{str(uuid.uuid4())[:12]}",
            pipeline_id=pipeline_id,
            source_id=source_id,
            source_ref=source_ref,
            metadata=metadata,
            created_at=datetime.utcnow(),
        )
        store.upsert(_to_doc(job, "job"))
        created += 1

    return {"created": created, "skipped": skipped}


@app.get("/api/jobs/stats")
def job_stats():
    """Get job statistics."""
    return get_job_stats()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Get a specific job."""
    store = get_store()
    doc = store.get(job_id, "job")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return _job_from_doc(doc)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Cancel a pending job."""
    store = get_store()
    doc = store.get(job_id, "job")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job = _job_from_doc(doc)
    if job.status != JobStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel job in '{job.status}' status"
        )

    job.status = JobStatus.CANCELLED
    store.upsert(_to_doc(job, "job"))
    return {"success": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Retry a failed job by resetting it to PENDING for worker pickup."""
    store = get_store()
    doc = store.get(job_id, "job")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job = _job_from_doc(doc)
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry job in '{job.status}' status"
        )

    MAX_MANUAL_RETRIES = 10
    if job.retry_count >= MAX_MANUAL_RETRIES:
        raise HTTPException(
            status_code=409,
            detail=f"Job has been retried {job.retry_count} times (max {MAX_MANUAL_RETRIES}). "
                   f"Investigate the root cause: {job.error}"
        )

    job.status = JobStatus.PENDING
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.retry_count += 1
    store.upsert(_to_doc(job, "job"))

    return {"success": True, "message": f"Job reset to PENDING (retry {job.retry_count}/{MAX_MANUAL_RETRIES})"}


# =============================================================================
# PLAYGROUND - VECTOR SEARCH
# =============================================================================

class SearchRequest(BaseModel):
    query: str
    destination_ids: List[str] = []
    top_k: int = 5
    per_index_top_k: Optional[int] = None
    merge_strategy: str = "interleave"  # "interleave" | "round_robin" | "per_index"
    destination_id: Optional[str] = None  # backward compat

    def __init__(self, **data):
        if "destination_id" in data and "destination_ids" not in data:
            data["destination_ids"] = [data.pop("destination_id")]
        super().__init__(**data)
        if self.top_k < 1:
            self.top_k = 1
        elif self.top_k > 100:
            self.top_k = 100
        if self.per_index_top_k is None:
            self.per_index_top_k = self.top_k


def _resolve_content_fields(content_field):
    """Normalize content_field to a list of field names."""
    if isinstance(content_field, list):
        return content_field
    if isinstance(content_field, str):
        return [content_field]
    return ["content"]


def _extract_text_from_doc(doc: dict, content_field) -> str:
    """Extract text from a document using one or more content fields, concatenated."""
    fields = _resolve_content_fields(content_field)
    parts = []
    for f in fields:
        val = doc.get(f)
        if val and isinstance(val, str):
            parts.append(val)
    if parts:
        return "\n\n".join(parts)
    # Fallback to common field names
    return doc.get("text") or doc.get("content") or ""


def _extract_text_parts_from_doc(doc: dict, content_field) -> list:
    """Extract labeled text parts from a document using one or more content fields."""
    fields = _resolve_content_fields(content_field)
    parts = []
    for f in fields:
        val = doc.get(f)
        if val and isinstance(val, str):
            parts.append({"field": f, "value": val})
    if not parts:
        fallback = doc.get("text") or doc.get("content") or ""
        if fallback:
            parts.append({"field": "content", "value": fallback})
    return parts


def _search_single_index(destination, query_embedding: list, top_k: int, content_field="content", vector_field=None) -> list:
    """Synchronous vector search on a single destination (CosmosDB or pgvector)."""
    from models import DestinationType

    # Check destination type and route to appropriate search function
    if destination.type == DestinationType.PGVECTOR:
        return _search_pgvector_index(destination, query_embedding, top_k, content_field, vector_field)
    else:
        return _search_cosmosdb_index(destination, query_embedding, top_k, content_field, vector_field)


def _search_pgvector_index(destination, query_embedding: list, top_k: int, content_field="content", vector_field=None) -> list:
    """Synchronous vector search on a pgvector table."""
    import asyncio
    from connectors.postgres_connector import search_vectors

    # Override vector_column in config if pipeline specifies a vector_index_path
    search_config = dict(destination.config)
    if vector_field:
        search_config["vector_column"] = vector_field

    raw_results = asyncio.run(
        search_vectors(search_config, query_embedding, top_k)
    )

    results = []
    for item in raw_results:
        result = {
            "id": item.get("id"),
            "source": item.get("metadata", {}).get("source"),
            "source_ref": item.get("metadata", {}).get("source_ref"),
            "text": item.get("content", ""),
            "metadata": item.get("metadata", {}),
            "score": item.get("similarity", 0),
            "distance": 1 - item.get("similarity", 0)
        }
        results.append(result)
    return results


def _search_cosmosdb_index(destination, query_embedding: list, top_k: int, content_field="content", vector_field=None) -> list:
    """Synchronous vector search on a single CosmosDB container."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    client = CosmosClient(destination.config["endpoint"], credential=credential)
    database = client.get_database_client(destination.config["database"])
    container = database.get_container_client(destination.config["container"])

    # Use pipeline's vector_index_path, fall back to destination config, then default
    vf = vector_field or destination.config.get("vector_field", "embedding")

    validated = [float(x) for x in query_embedding]
    embedding_str = "[" + ",".join(str(x) for x in validated) + "]"

    query = f"""
        SELECT TOP @top_k
            c, VectorDistance(c.{vf}, {embedding_str}) as distance
        FROM c
        ORDER BY VectorDistance(c.{vf}, {embedding_str})
    """

    fields = _resolve_content_fields(content_field)
    results = []
    for item in container.query_items(
        query=query,
        parameters=[{"name": "@top_k", "value": top_k}],
        enable_cross_partition_query=True
    ):
        distance = item.get("distance", 0)
        doc = item.get("c", {})
        text = _extract_text_from_doc(doc, content_field)
        text_parts = _extract_text_parts_from_doc(doc, content_field)
        # Build metadata from scalar fields
        skip_fields = {"id", "text", "content", "source", "source_ref",
                       "embedding", "metadata", "_rid", "_self", "_etag", "_attachments", "_ts",
                       vf}
        skip_fields.update(fields)
        metadata = doc.get("metadata", {})
        for k, v in doc.items():
            if k not in skip_fields and not isinstance(v, (list, dict)):
                metadata[k] = v
        result = {
            "id": doc.get("id"),
            "source": doc.get("source"),
            "source_ref": doc.get("source_ref") or doc.get("title") or doc.get("url"),
            "text": text,
            "metadata": metadata,
            "score": 1 - distance,
            "distance": distance
        }
        if len(text_parts) > 1:
            result["text_parts"] = text_parts
        results.append(result)
    return results


def _merge_results(index_results: list, strategy: str, top_k: int) -> list:
    """Merge results from multiple indexes."""
    if strategy == "round_robin":
        merged = []
        iterators = [iter(ir["results"]) for ir in index_results if ir.get("results")]
        while len(merged) < top_k and iterators:
            exhausted = []
            for i, it in enumerate(iterators):
                if len(merged) >= top_k:
                    break
                try:
                    merged.append(next(it))
                except StopIteration:
                    exhausted.append(i)
            for i in reversed(exhausted):
                iterators.pop(i)
        return merged
    else:
        # "interleave" (default) and "per_index": pool + sort by score
        all_results = []
        for ir in index_results:
            all_results.extend(ir.get("results", []))
        all_results.sort(key=lambda r: r["score"], reverse=True)
        if strategy != "per_index":
            all_results = all_results[:top_k]
        return all_results


@app.post("/api/playground/search")
async def playground_search(req: SearchRequest):
    """Search vectors across one or more indexes using query embedding."""
    import time

    _search_start = time.time()

    if not req.destination_ids:
        raise HTTPException(status_code=400, detail="No destination_ids provided")
    if len(req.destination_ids) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 indexes per search")

    store = get_store()

    # Load all destinations and find their pipelines
    pipeline_docs = await asyncio.to_thread(store.list, "pipeline")
    dest_infos = []  # [(dest_id, destination, pipeline_doc)]
    for dest_id in req.destination_ids:
        doc = await asyncio.to_thread(store.get, dest_id, "destination")
        if not doc:
            continue
        destination = _destination_from_doc(doc)
        # Find pipeline for this destination
        matched = None
        for pd in pipeline_docs:
            if pd.get("destination_id") == dest_id and pd.get("status") == "active":
                matched = pd
                break
        if not matched:
            for pd in pipeline_docs:
                if pd.get("destination_id") == dest_id:
                    matched = pd
                    break
        dest_infos.append((dest_id, destination, matched))

    if not dest_infos:
        raise HTTPException(status_code=404, detail="No valid destinations found")

    # Group by embedding model and generate embeddings
    from collections import defaultdict
    model_groups = defaultdict(list)
    for dest_id, dest, pip in dest_infos:
        model_ref = pip["docgrok_pipeline"] if pip else None
        model_groups[model_ref].append((dest_id, dest, pip))

    embed_start = time.time()
    embeddings = {}  # {model_ref: embedding_vector}
    warnings = []

    for model_ref in model_groups:
        if model_ref is None:
            for dest_id, _, _ in model_groups[model_ref]:
                warnings.append(f"No pipeline for destination '{dest_id}', skipping")
            continue
        try:
            if model_ref.startswith("mdl-"):
                embed_body = {"model_id": model_ref, "text": req.query, "requestId": f"search-{int(time.time())}"}
            else:
                embed_body = {"pipeline": model_ref, "text": req.query, "requestId": f"search-{int(time.time())}"}

            resp = await http_client.post(f"{DOCGROK_URL}/embed", json=embed_body, timeout=30.0)
            if resp.status_code != 200:
                warnings.append(f"Embedding failed for model {model_ref}: {resp.text[:100]}")
                continue
            embed_result = resp.json()
            query_embedding = (embed_result.get("pages") or embed_result.get("output") or [[]])[0]
            if query_embedding:
                embeddings[model_ref] = query_embedding
        except Exception as e:
            warnings.append(f"Embedding error for {model_ref}: {str(e)[:100]}")

    embed_time = int((time.time() - embed_start) * 1000)

    if not embeddings:
        raise HTTPException(status_code=503, detail="Failed to generate embeddings for any model")

    if len(embeddings) > 1 and req.merge_strategy == "interleave":
        warnings.append("Merging results across different embedding models â€” scores may not be directly comparable")

    # Search all indexes in parallel
    search_start = time.time()

    # Resolve content_fields and vector_index_path per destination from pipeline config
    source_content_fields = {}  # dest_id -> content_fields
    dest_vector_fields = {}  # dest_id -> vector_index_path
    for dest_id, dest, pip in dest_infos:
        cf = ["content"]
        if pip:
            sources = pip.get("sources", [])
            if sources:
                src = sources[0] if isinstance(sources[0], dict) else sources[0].dict()
                cf = src.get("content_fields", ["content"])
            vip = pip.get("vector_index_path")
            if vip:
                dest_vector_fields[dest_id] = vip.lstrip("/")
        source_content_fields[dest_id] = cf

    async def search_one(dest_id, destination, model_ref):
        try:
            t0 = time.time()
            cf = source_content_fields.get(dest_id, ["content"])
            vf = dest_vector_fields.get(dest_id)
            results = await asyncio.to_thread(
                _search_single_index, destination, embeddings[model_ref], req.per_index_top_k, cf, vf
            )
            search_ms = int((time.time() - t0) * 1000)
            for r in results:
                r["index_id"] = dest_id
                r["index_name"] = destination.name
            return {"dest_id": dest_id, "dest_name": destination.name, "model_ref": model_ref,
                    "results": results, "result_count": len(results), "search_time_ms": search_ms}
        except Exception as e:
            return {"dest_id": dest_id, "dest_name": destination.name, "model_ref": model_ref,
                    "results": [], "result_count": 0, "error": str(e)[:200]}

    search_tasks = []
    for model_ref, dest_list in model_groups.items():
        if model_ref not in embeddings:
            continue
        for dest_id, dest, pip in dest_list:
            search_tasks.append(search_one(dest_id, dest, model_ref))

    index_results = await asyncio.gather(*search_tasks)
    total_search_time = int((time.time() - search_start) * 1000)

    # Merge results
    merged = _merge_results(list(index_results), req.merge_strategy, req.top_k)

    # Build per-index metadata
    indexes_searched = []
    for ir in index_results:
        entry = {"index_id": ir["dest_id"], "index_name": ir["dest_name"],
                 "result_count": ir["result_count"], "search_time_ms": ir.get("search_time_ms", 0),
                 "embedding_model": ir.get("model_ref", "")}
        if "error" in ir:
            entry["error"] = ir["error"]
        indexes_searched.append(entry)

    embedding_dims = len(next(iter(embeddings.values()))) if embeddings else 0

    total_latency_ms = int((time.time() - _search_start) * 1000)
    record_search(
        latency_ms=total_latency_ms,
        embed_latency_ms=embed_time,
        tokens_used=0,  # TODO: parse from embed_result.get("usage", {}).get("total_tokens", 0)
        results_count=len(merged),
    )

    return {
        "query": req.query,
        "results": merged,
        "merge_strategy": req.merge_strategy,
        "indexes_searched": indexes_searched,
        "embedding_dims": embedding_dims,
        "embedding_time_ms": embed_time,
        "total_search_time_ms": total_search_time,
        "warnings": warnings if warnings else None
    }


# =============================================================================
# EVENT GRID WEBHOOK (Continuous Processing)
# =============================================================================

@app.post("/api/webhooks/eventgrid")
async def eventgrid_webhook(request: Request):
    """
    Handle Azure Event Grid events for blob storage changes.
    Supports both Event Grid schema and CloudEvents schema.
    """
    body = await request.json()

    # Handle Event Grid validation handshake
    if isinstance(body, list) and len(body) > 0:
        first_event = body[0]
        # Subscription validation
        if first_event.get("eventType") == "Microsoft.EventGrid.SubscriptionValidationEvent":
            validation_code = first_event.get("data", {}).get("validationCode")
            print(f"Event Grid validation request received, code: {validation_code}")
            return JSONResponse({"validationResponse": validation_code})

    # Process events
    events = body if isinstance(body, list) else [body]
    processed = 0

    for event in events:
        event_type = event.get("eventType", event.get("type", ""))
        event_data = event.get("data", {})

        # Handle blob events
        if "BlobCreated" in event_type or "BlobModified" in event_type:
            blob_url = event_data.get("url", "")
            content_type = event_data.get("contentType", "")

            if blob_url:
                await EVENT_QUEUE.put({
                    "type": "blob_created",
                    "url": blob_url,
                    "content_type": content_type,
                    "event_time": event.get("eventTime", datetime.utcnow().isoformat())
                })
                processed += 1
                print(f"Queued blob event: {blob_url}")

        elif "BlobDeleted" in event_type:
            blob_url = event_data.get("url", "")
            if blob_url:
                await EVENT_QUEUE.put({
                    "type": "blob_deleted",
                    "url": blob_url,
                    "event_time": event.get("eventTime", datetime.utcnow().isoformat())
                })
                processed += 1
                print(f"Queued blob delete event: {blob_url}")

    return {"success": True, "events_queued": processed}


@app.get("/api/webhooks/eventgrid")
async def eventgrid_webhook_validation(request: Request):
    """Handle Event Grid webhook validation (OPTIONS/GET for CloudEvents)."""
    webhook_callback = request.headers.get("WebHook-Request-Callback")
    if webhook_callback:
        return JSONResponse(
            content={"message": "Webhook validated"},
            headers={"WebHook-Allowed-Origin": request.headers.get("WebHook-Request-Origin", "*")}
        )
    return {"status": "ready"}


async def event_processor_worker():
    """Background worker that processes events from the queue."""
    print("Event processor worker started")
    while True:
        try:
            event = await EVENT_QUEUE.get()
            await process_event(event)
            EVENT_QUEUE.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error processing event: {e}")


async def process_event(event: dict):
    """Process a single event (blob created/deleted)."""
    event_type = event.get("type")
    blob_url = event.get("url", "")

    if not blob_url:
        return

    # Parse blob URL: https://<account>.blob.core.windows.net/<container>/<blob_path>
    try:
        from urllib.parse import urlparse
        parsed = urlparse(blob_url)
        host_parts = parsed.netloc.split(".")
        account_name = host_parts[0] if host_parts else ""
        path_parts = parsed.path.lstrip("/").split("/", 1)
        container_name = path_parts[0] if path_parts else ""
        blob_path = path_parts[1] if len(path_parts) > 1 else ""
    except Exception as e:
        print(f"Failed to parse blob URL {blob_url}: {e}")
        return

    # Get file extension
    file_ext = os.path.splitext(blob_path)[1].lower().lstrip('.')

    # Find matching sources
    store = get_store()
    all_sources = [_source_from_doc(d) for d in store.list("source")]
    matching_sources = []
    for source in all_sources:
        if source.type != SourceType.AZURE_BLOB:
            continue

        source_account = source.config.get("account_url", "")
        source_container = source.config.get("container", "")
        source_prefix = source.config.get("prefix", "")

        if account_name in source_account and container_name == source_container:
            if not source_prefix or blob_path.startswith(source_prefix):
                matching_sources.append(source)

    if not matching_sources:
        print(f"No matching sources for blob: {blob_url}")
        return

    # Find pipelines using these sources
    all_pipelines = [_pipeline_from_doc(d) for d in store.list("pipeline")]
    for source in matching_sources:
        for pipeline in all_pipelines:
            if pipeline.status != PipelineStatus.ACTIVE:
                continue

            pipeline_source = next((ps for ps in pipeline.sources if ps.source_id == source.id), None)
            if not pipeline_source:
                continue

            # Check file_types from pipeline source config
            allowed_types = pipeline_source.file_types
            if file_ext not in allowed_types:
                print(f"Skipping blob {blob_path}: file type '{file_ext}' not in allowed types {allowed_types} for pipeline {pipeline.name}")
                continue

            if event_type == "blob_created":
                job = Job(
                    id=f"job-{str(uuid.uuid4())[:12]}",
                    pipeline_id=pipeline.id,
                    source_id=source.id,
                    source_ref=blob_path,
                    metadata={"trigger": "eventgrid", "event_time": event.get("event_time"), "file_type": file_ext},
                    created_at=datetime.utcnow()
                )
                store.upsert(_to_doc(job, "job"))
                print(f"Created job {job.id} for blob {blob_path} (type: {file_ext}) via pipeline {pipeline.name}")

                await process_job(job)

            elif event_type == "blob_deleted":
                print(f"Blob deleted: {blob_path} - vector removal not yet implemented")


# =============================================================================
# TRIGGER STATUS
# =============================================================================

@app.get("/api/triggers/status")
def get_triggers_status():
    """Get status of all continuous processing triggers.

    Note: Change Feed processing has moved to the controller/worker.
    This endpoint reports source configuration status.
    """
    store = get_store()
    blob_sources = []
    cosmosdb_sources = []

    for d in store.list("source"):
        source = _source_from_doc(d)
        if source.type == SourceType.AZURE_BLOB:
            blob_sources.append({
                "id": source.id,
                "name": source.name,
                "trigger_type": "eventgrid",
                "status": "configured" if source.triggers else "not_configured"
            })
        elif source.type == SourceType.COSMOSDB:
            cosmosdb_sources.append({
                "id": source.id,
                "name": source.name,
                "trigger_type": "change_feed",
                "status": "managed_by_controller"
            })

    return {
        "blob_sources": blob_sources,
        "cosmosdb_sources": cosmosdb_sources,
        "event_queue_size": EVENT_QUEUE.qsize() if EVENT_QUEUE else 0
    }


# =============================================================================
# EVENT GRID SUBSCRIPTION MANAGEMENT
# =============================================================================

EVENT_GRID_SUBSCRIPTIONS: dict[str, str] = {}


class CreateEventGridRequest(BaseModel):
    source_id: str
    subscription_name: Optional[str] = None


@app.post("/api/triggers/eventgrid/create")
async def create_eventgrid_subscription(req: CreateEventGridRequest):
    """Create an Event Grid subscription for a blob storage source."""
    store = get_store()
    doc = store.get(req.source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{req.source_id}' not found")

    source = _source_from_doc(doc)
    if source.type != SourceType.AZURE_BLOB:
        raise HTTPException(status_code=400, detail="Event Grid is only for blob storage sources")

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.eventgrid import EventGridManagementClient
        from azure.mgmt.storage import StorageManagementClient
        from urllib.parse import urlparse

        credential = DefaultAzureCredential()

        account_url = source.config.get("account_url", "")
        parsed = urlparse(account_url)
        account_name = parsed.netloc.split(".")[0]
        container_name = source.config.get("container", "")

        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            try:
                resp = await http_client.get(
                    "http://169.254.169.254/metadata/instance/compute/subscriptionId?api-version=2021-02-01",
                    headers={"Metadata": "true"},
                    timeout=5.0
                )
                subscription_id = resp.text.strip('"')
            except:
                pass

        if not subscription_id:
            return {
                "success": False,
                "error": "AZURE_SUBSCRIPTION_ID environment variable not set",
                "manual_setup": get_eventgrid_setup_commands(account_name, container_name, source.id)
            }

        storage_client = StorageManagementClient(credential, subscription_id)

        storage_account = None
        resource_group = None
        for account in storage_client.storage_accounts.list():
            if account.name == account_name:
                storage_account = account
                parts = account.id.split("/")
                rg_idx = parts.index("resourceGroups") + 1
                resource_group = parts[rg_idx]
                break

        if not storage_account:
            return {
                "success": False,
                "error": f"Storage account '{account_name}' not found in subscription",
                "manual_setup": get_eventgrid_setup_commands(account_name, container_name, source.id)
            }

        eventgrid_client = EventGridManagementClient(credential, subscription_id)

        subscription_name = req.subscription_name or f"omnivec-{source.id}"

        webhook_url = os.getenv("OMNIVEC_WEBHOOK_URL")
        if not webhook_url:
            svc_ip = os.getenv("OMNIVEC_EXTERNAL_IP", "")
            if svc_ip:
                webhook_url = f"http://{svc_ip}/api/webhooks/eventgrid"
            else:
                return {
                    "success": False,
                    "error": "OMNIVEC_WEBHOOK_URL or OMNIVEC_EXTERNAL_IP environment variable not set",
                    "manual_setup": get_eventgrid_setup_commands(account_name, container_name, source.id)
                }

        subject_filter = {
            "subjectBeginsWith": f"/blobServices/default/containers/{container_name}/",
            "subjectEndsWith": "",
            "isSubjectCaseSensitive": False
        }

        prefix = source.config.get("prefix", "")
        if prefix:
            subject_filter["subjectBeginsWith"] = f"/blobServices/default/containers/{container_name}/blobs/{prefix}"

        event_subscription = {
            "destination": {
                "endpointType": "WebHook",
                "properties": {
                    "endpointUrl": webhook_url
                }
            },
            "filter": {
                "includedEventTypes": [
                    "Microsoft.Storage.BlobCreated",
                    "Microsoft.Storage.BlobDeleted"
                ],
                **subject_filter
            }
        }

        result = eventgrid_client.event_subscriptions.begin_create_or_update(
            scope=storage_account.id,
            event_subscription_name=subscription_name,
            event_subscription_info=event_subscription
        ).result()

        EVENT_GRID_SUBSCRIPTIONS[account_name] = subscription_name

        source.triggers = source.triggers or []
        if "eventgrid" not in source.triggers:
            source.triggers.append("eventgrid")
        store.upsert(_to_doc(source, "source"))

        return {
            "success": True,
            "message": f"Event Grid subscription '{subscription_name}' created",
            "subscription_id": result.id,
            "storage_account": account_name,
            "container": container_name,
            "webhook_url": webhook_url
        }

    except Exception as e:
        error_msg = str(e)
        account_url = source.config.get("account_url", "")
        parsed_url = urlparse(account_url) if account_url else None
        account_name = parsed_url.netloc.split(".")[0] if parsed_url else "<storage-account>"
        container_name = source.config.get("container", "<container>")

        return {
            "success": False,
            "error": error_msg,
            "manual_setup": get_eventgrid_setup_commands(account_name, container_name, source.id)
        }


@app.delete("/api/triggers/eventgrid/{source_id}")
async def delete_eventgrid_subscription(source_id: str):
    """Delete Event Grid subscription for a source."""
    store = get_store()
    doc = store.get(source_id, "source")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")

    source = _source_from_doc(doc)

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.eventgrid import EventGridManagementClient
        from azure.mgmt.storage import StorageManagementClient
        from urllib.parse import urlparse

        credential = DefaultAzureCredential()
        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")

        if not subscription_id:
            return {"success": False, "error": "AZURE_SUBSCRIPTION_ID not set"}

        account_url = source.config.get("account_url", "")
        parsed = urlparse(account_url)
        account_name = parsed.netloc.split(".")[0]

        subscription_name = f"omnivec-{source_id}"

        storage_client = StorageManagementClient(credential, subscription_id)
        storage_account = None
        for account in storage_client.storage_accounts.list():
            if account.name == account_name:
                storage_account = account
                break

        if not storage_account:
            return {"success": False, "error": f"Storage account '{account_name}' not found"}

        eventgrid_client = EventGridManagementClient(credential, subscription_id)
        eventgrid_client.event_subscriptions.begin_delete(
            scope=storage_account.id,
            event_subscription_name=subscription_name
        ).result()

        EVENT_GRID_SUBSCRIPTIONS.pop(account_name, None)

        if source.triggers and "eventgrid" in source.triggers:
            source.triggers.remove("eventgrid")
        store.upsert(_to_doc(source, "source"))

        return {"success": True, "message": f"Event Grid subscription '{subscription_name}' deleted"}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/triggers/eventgrid/list")
async def list_eventgrid_subscriptions():
    """List all Event Grid subscriptions for blob sources."""
    subscriptions = []

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.eventgrid import EventGridManagementClient

        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            return {"subscriptions": [], "error": "AZURE_SUBSCRIPTION_ID not set"}

        credential = DefaultAzureCredential()
        client = EventGridManagementClient(credential, subscription_id)

        for sub in client.event_subscriptions.list_global_by_subscription():
            if sub.name.startswith("omnivec-"):
                subscriptions.append({
                    "name": sub.name,
                    "id": sub.id,
                    "provisioning_state": sub.provisioning_state,
                    "destination": sub.destination.endpoint_url if hasattr(sub.destination, 'endpoint_url') else None
                })

    except Exception as e:
        return {"subscriptions": subscriptions, "error": str(e)}

    return {"subscriptions": subscriptions}


def get_eventgrid_setup_commands(storage_account: str, container: str, source_id: str) -> dict:
    """Generate manual setup commands for Event Grid."""
    webhook_url = os.getenv("OMNIVEC_WEBHOOK_URL", "http://<omnivec-ip>/api/webhooks/eventgrid")

    return {
        "az_cli": f"""# Create Event Grid subscription
az eventgrid event-subscription create \\
  --name omnivec-{source_id} \\
  --source-resource-id $(az storage account show -n {storage_account} --query id -o tsv) \\
  --endpoint {webhook_url} \\
  --endpoint-type webhook \\
  --included-event-types Microsoft.Storage.BlobCreated Microsoft.Storage.BlobDeleted \\
  --subject-begins-with /blobServices/default/containers/{container}/""",
        "powershell": f"""# Create Event Grid subscription
$storageId = (az storage account show -n {storage_account} --query id -o tsv)
az eventgrid event-subscription create `
  --name omnivec-{source_id} `
  --source-resource-id $storageId `
  --endpoint {webhook_url} `
  --endpoint-type webhook `
  --included-event-types Microsoft.Storage.BlobCreated Microsoft.Storage.BlobDeleted `
  --subject-begins-with /blobServices/default/containers/{container}/"""
    }


# =============================================================================
# OPERATIONS â€” K8s Deployment Management
# =============================================================================

OMNIVEC_DEPLOYMENTS = ["omnivec-web", "omnivec-api", "omnivec-controller", "omnivec-worker", "omnivec-source-connector", "omnivec-dotnet-worker"]
OMNIVEC_NAMESPACE = "omnivec"

_k8s_apps_v1 = None
_k8s_core_v1 = None
_k8s_autoscaling_v2 = None


def _get_k8s_clients():
    global _k8s_apps_v1, _k8s_core_v1, _k8s_autoscaling_v2
    if _k8s_apps_v1 is None:
        from kubernetes import client, config
        config.load_incluster_config()
        _k8s_apps_v1 = client.AppsV1Api()
        _k8s_core_v1 = client.CoreV1Api()
        _k8s_autoscaling_v2 = client.AutoscalingV2Api()
    return _k8s_apps_v1, _k8s_core_v1, _k8s_autoscaling_v2


def _age_str(created):
    """Human-readable age from a datetime."""
    if not created:
        return "unknown"
    delta = datetime.utcnow() - created.replace(tzinfo=None)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h{minutes % 60}m"
    days = hours // 24
    return f"{days}d{hours % 24}h"


@app.get("/api/operations/deployments")
async def get_operations_deployments():
    """List OmniVec deployments with pod details."""
    try:
        apps_v1, core_v1, autoscaling_v2 = _get_k8s_clients()
        result = []

        # Pre-fetch all HPAs in namespace
        hpa_map = {}
        try:
            hpa_list = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(OMNIVEC_NAMESPACE)
            for hpa in hpa_list.items:
                target = hpa.spec.scale_target_ref
                if target:
                    hpa_map[target.name] = hpa
        except Exception as e:
            logger.warning("Failed to list HPAs: %s", e)

        for dep_name in OMNIVEC_DEPLOYMENTS:
            try:
                dep = apps_v1.read_namespaced_deployment(dep_name, OMNIVEC_NAMESPACE)
            except Exception:
                continue

            # Get image from first container
            containers = dep.spec.template.spec.containers or []
            image = containers[0].image if containers else "unknown"

            # Get pods for this deployment
            label_selector = f"app={dep_name}"
            pods_list = core_v1.list_namespaced_pod(OMNIVEC_NAMESPACE, label_selector=label_selector)

            pods = []
            for pod in pods_list.items:
                restarts = 0
                if pod.status.container_statuses:
                    restarts = sum(cs.restart_count for cs in pod.status.container_statuses)
                pods.append({
                    "name": pod.metadata.name,
                    "status": pod.status.phase,
                    "restarts": restarts,
                    "age": _age_str(pod.metadata.creation_timestamp),
                })

            ready = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 0
            available = dep.status.available_replicas or 0

            if desired == 0:
                status = "Stopped"
            elif ready < desired:
                status = "Degraded"
            else:
                status = "Running"

            dep_info = {
                "name": dep_name,
                "replicas": desired,
                "ready_replicas": ready,
                "available_replicas": available,
                "status": status,
                "image": image,
                "pods": pods,
            }

            # Add HPA info if autoscaling is configured
            hpa = hpa_map.get(dep_name)
            if hpa:
                current_cpu = None
                target_cpu = None
                if hpa.status and hpa.status.current_metrics:
                    for m in hpa.status.current_metrics:
                        if m.type == "Resource" and m.resource and m.resource.name == "cpu":
                            current_cpu = m.resource.current.average_utilization
                if hpa.spec.metrics:
                    for m in hpa.spec.metrics:
                        if m.type == "Resource" and m.resource and m.resource.name == "cpu":
                            target_cpu = m.resource.target.average_utilization

                max_replicas = hpa.spec.max_replicas
                min_replicas = hpa.spec.min_replicas
                at_max = desired >= max_replicas
                saturated = at_max and current_cpu is not None and target_cpu is not None and current_cpu > target_cpu

                dep_info["autoscaling"] = {
                    "enabled": True,
                    "min_replicas": min_replicas,
                    "max_replicas": max_replicas,
                    "current_cpu_percent": current_cpu,
                    "target_cpu_percent": target_cpu,
                    "at_max_replicas": at_max,
                    "saturated": saturated,
                }
                if saturated:
                    dep_info["status"] = "Saturated"

            result.append(dep_info)

        return result
    except Exception as e:
        logger.error("Failed to get deployments: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/operations/deployments/{name}/scale")
async def scale_deployment(name: str, payload: dict):
    """Scale a deployment. Body: {"replicas": N, "max_replicas": N (optional, updates HPA)}
    Applies to K8s immediately AND persists to CosmosDB so it survives redeployments."""
    if name not in OMNIVEC_DEPLOYMENTS:
        raise HTTPException(status_code=400, detail=f"Unknown deployment: {name}")

    replicas = payload.get("replicas")
    max_replicas = payload.get("max_replicas")

    if replicas is not None and (not isinstance(replicas, int) or replicas < 0):
        raise HTTPException(status_code=400, detail="replicas must be a non-negative integer")
    if max_replicas is not None and (not isinstance(max_replicas, int) or max_replicas < 1):
        raise HTTPException(status_code=400, detail="max_replicas must be a positive integer")
    if replicas is None and max_replicas is None:
        raise HTTPException(status_code=400, detail="Provide replicas and/or max_replicas")

    try:
        apps_v1, _, autoscaling_v2 = _get_k8s_clients()
        messages = []

        # For deployments with HPA (changefeed, worker), also update HPA min
        has_hpa = name in ("omnivec-source-connector", "omnivec-worker")

        if replicas is not None:
            apps_v1.patch_namespaced_deployment_scale(
                name, OMNIVEC_NAMESPACE,
                body={"spec": {"replicas": replicas}},
            )
            messages.append(f"Scaled {name} to {replicas} replicas")

            # Update HPA min to match so it doesn't scale back down
            if has_hpa:
                try:
                    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                        name, OMNIVEC_NAMESPACE,
                        body={"spec": {"minReplicas": replicas}},
                    )
                    messages.append(f"Updated HPA min replicas to {replicas}")
                except Exception:
                    pass

        if max_replicas is not None:
            try:
                autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                    name, OMNIVEC_NAMESPACE,
                    body={"spec": {"maxReplicas": max_replicas}},
                )
                messages.append(f"Updated HPA max replicas to {max_replicas}")
            except Exception as hpa_err:
                messages.append(f"Failed to update HPA: {str(hpa_err)[:100]}")

        # Persist to CosmosDB so changes survive redeployments
        try:
            _persist_scale_config(name, replicas, max_replicas)
            messages.append("Persisted to CosmosDB")
        except Exception as pe:
            logger.warning("Failed to persist scale config: %s", pe)

        return {"success": True, "message": "; ".join(messages)}
    except Exception as e:
        logger.error("Failed to scale %s: %s", name, e)
        raise HTTPException(status_code=500, detail=str(e))


def _persist_scale_config(dep_name: str, replicas: int | None, max_replicas: int | None):
    """Save scale change to CosmosDB operational config."""
    # Map deployment name to settings keys
    key_map = {
        "omnivec-source-connector": {"replicas": "changefeed.replicas"},
        "omnivec-worker":     {"replicas": "worker.minReplicas", "max_replicas": "worker.maxReplicas"},
        "omnivec-controller": {"replicas": "controller.replicas"},
        "omnivec-api":        {"replicas": "api.replicas"},
        "omnivec-web":        {"replicas": "web.replicas"},
    }
    mapping = key_map.get(dep_name, {})
    if not mapping:
        return

    store = get_store()
    try:
        doc = store.get(CONFIG_DOC_ID, "config")
        if not doc:
            doc = {"id": CONFIG_DOC_ID, "doc_type": "config"}
    except Exception:
        doc = {"id": CONFIG_DOC_ID, "doc_type": "config"}

    if replicas is not None and "replicas" in mapping:
        doc[mapping["replicas"]] = replicas
    if max_replicas is not None and "max_replicas" in mapping:
        doc[mapping["max_replicas"]] = max_replicas
    doc["updated_at"] = datetime.utcnow().isoformat()
    doc["updated_by"] = "ui"
    store.upsert(doc)


@app.post("/api/operations/deployments/{name}/restart")
async def restart_deployment(name: str):
    """Rolling restart a deployment by patching template annotation."""
    if name not in OMNIVEC_DEPLOYMENTS:
        raise HTTPException(status_code=400, detail=f"Unknown deployment: {name}")

    try:
        apps_v1, _, _autoscaling = _get_k8s_clients()
        now = datetime.utcnow().isoformat()
        apps_v1.patch_namespaced_deployment(
            name, OMNIVEC_NAMESPACE,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"omnivec/restartedAt": now}
                        }
                    }
                }
            },
        )
        return {"success": True, "message": f"Restarting {name}"}
    except Exception as e:
        logger.error("Failed to restart %s: %s", name, e)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# CHANGEFEED PARTITION LEASES
# =============================================================================

def _cfp_generation(reset_at) -> str:
    """Compute CFP generation tag from reset_at â€” must match .NET GetGeneration()."""
    if not reset_at:
        return "0"
    import hashlib
    from datetime import datetime as dt
    # Must produce the same string as FastAPI JSON serialization (.isoformat())
    # which is what the .NET CFP sees and hashes
    s = reset_at.isoformat() if isinstance(reset_at, dt) else str(reset_at)
    h = hashlib.sha256(s.encode()).hexdigest()[:8]
    return h


@app.get("/api/operations/changefeed/leases")
def get_changefeed_leases():
    """Return partition lease assignments for all CosmosDB sources (current generation only)."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    store = get_store()
    sources = [
        d for d in store.list("source")
        if d.get("type") == "cosmosdb"
    ]

    # Build source_id â†’ latest generation from active pipelines
    pipelines = store.list("pipeline")
    source_generations = {}
    for p in pipelines:
        if p.get("status") != "active":
            continue
        gen = _cfp_generation(p.get("reset_at"))
        for ps in p.get("sources", []):
            sid = ps.get("source_id", "")
            # Keep the latest (alphabetically greatest) generation per source
            if sid not in source_generations or gen > source_generations[sid]:
                source_generations[sid] = gen

    result = []
    credential = DefaultAzureCredential()
    cosmos_endpoint = os.getenv("COSMOS_ENDPOINT", "https://omnivec-cosmos.documents.azure.com:443/")
    client = CosmosClient(cosmos_endpoint, credential=credential)
    db = client.get_database_client(os.getenv("COSMOS_DATABASE", "omnivec"))

    for src in sources:
        source_id = src["id"]
        container_name = f"leases-{source_id}"
        gen = source_generations.get(source_id, "0")
        # CFP processorName prefix for this generation
        gen_prefix = f"omnivec-cf-{source_id}-gen{gen}"
        try:
            container = db.get_container_client(container_name)
            items = list(container.query_items(
                "SELECT c.id, c.LeaseToken, c.Owner, c.ContinuationToken, c.timestamp, c.FeedRange FROM c",
                enable_cross_partition_query=True,
            ))
            leases = []
            for item in items:
                if not item.get("LeaseToken"):
                    continue
                # Only include leases from the current generation
                doc_id = item.get("id", "")
                if not doc_id.startswith(gen_prefix):
                    continue
                leases.append({
                    "partition": item.get("LeaseToken", ""),
                    "owner": item.get("Owner", ""),
                    "continuation_token": item.get("ContinuationToken", ""),
                    "timestamp": item.get("timestamp", ""),
                    "feed_range": item.get("FeedRange", {}).get("Range", {}),
                })
            leases.sort(key=lambda l: int(l["partition"]) if l["partition"].isdigit() else 0)
            result.append({
                "source_id": source_id,
                "source_name": src.get("name", ""),
                "lease_container": container_name,
                "generation": gen,
                "partitions": len(leases),
                "leases": leases,
            })
        except Exception as e:
            logger.warning("Failed to read leases for %s: %s", source_id, e)
            result.append({
                "source_id": source_id,
                "source_name": src.get("name", ""),
                "lease_container": container_name,
                "generation": gen,
                "partitions": 0,
                "leases": [],
                "error": str(e),
            })

    return result


# =============================================================================
# MODELS â€” proxied to DocGrok (DocGrok owns the model registry)
# =============================================================================

@app.get("/api/models")
async def list_models():
    """List all models â€” proxied from DocGrok model registry, enriched with model_category."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/models/registry")
        data = resp.json()
        models = data.get("models", [])

        # Load stored model metadata from CosmosDB to get model_category
        store = get_store()
        stored = {}
        try:
            for doc in store.query(
                "SELECT c.id, c.model_category FROM c WHERE c.doc_type = 'docgrok_model'",
                partition_key="docgrok_model",
            ):
                stored[doc["id"]] = doc.get("model_category", "embedding")
        except Exception:
            pass

        # Add chat-only models from CosmosDB that aren't in DocGrok
        docgrok_ids = {m.get("id") for m in models}
        try:
            for doc in store.query(
                "SELECT * FROM c WHERE c.doc_type = 'docgrok_model' AND c.model_category = 'chat'",
                partition_key="docgrok_model",
            ):
                if doc["id"] not in docgrok_ids:
                    models.append({
                        "id": doc["id"],
                        "name": doc.get("name", ""),
                        "kind": "external",
                        "type": doc.get("type", "azure-openai"),
                        "endpoint": doc.get("endpoint", ""),
                        "deployment": doc.get("deployment", ""),
                        "embedding_dim": doc.get("embedding_dim", 0),
                        "api_version": doc.get("api_version", ""),
                        "model_category": "chat",
                    })

        except Exception:
            pass

        # Enrich all models with model_category (default to "embedding" for existing)
        # Mask sensitive fields (api_key, secret) from responses
        for m in models:
            if "model_category" not in m:
                m["model_category"] = stored.get(m.get("id"), "embedding")
            for sensitive_key in ("api_key", "secret", "token"):
                if sensitive_key in m and m[sensitive_key]:
                    m[sensitive_key] = "***"

        data["models"] = models
        return data
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/models")
async def create_model(payload: dict):
    """Create an external model â€” proxied to DocGrok, persisted in CosmosDB."""
    try:
        # Map UI field names to DocGrok registry fields
        model_name = payload.get("name", payload.get("model", "")).strip()
        model_category = payload.get("model_category", "embedding")  # "embedding" or "chat"
        auth_type = payload.get("auth_type", "key")  # "key" or "managed-identity"
        reg_payload = {
            "name": model_name,
            "type": payload.get("provider_type", payload.get("type", "azure-openai")),
            "endpoint": payload.get("endpoint", "").strip(),
            "auth_type": auth_type,
            "api_key": payload.get("api_key", "").strip(),
            "deployment": payload.get("deployment", payload.get("name", "")).strip(),
            "embedding_dim": int(payload.get("embedding_dim", payload.get("dimensions", 1536))),
            "api_version": payload.get("api_version", "2024-06-01"),
        }
        if auth_type == "managed-identity":
            client_id = payload.get("client_id", "").strip()
            if client_id:
                reg_payload["client_id"] = client_id
        # Preserve stored ID â€” look up by name in CosmosDB so DocGrok always
        # gets the same ID even after restart (prevents ID drift).
        store = get_store()
        try:
            existing = store.query(
                "SELECT c.id FROM c WHERE c.doc_type = 'docgrok_model' AND c.name = @name",
                partition_key="docgrok_model",
                parameters=[{"name": "@name", "value": model_name}],
            )
            for doc in existing:
                reg_payload["id"] = doc["id"]
                break
        except Exception:
            pass

        # Store API key in Key Vault (if configured), strip from CosmosDB doc
        from keyvault_client import set_model_api_key
        api_key_value = reg_payload.get("api_key", "")

        # For chat models, skip DocGrok registration (DocGrok only handles embeddings)
        if model_category == "chat":
            model_id = reg_payload.get("id") or f"mdl-ext-{str(uuid.uuid4())[:8]}"
            # Store key in Key Vault, remove from CosmosDB doc
            persist_doc = {k: v for k, v in reg_payload.items() if k != "api_key"}
            if api_key_value and set_model_api_key(model_id, api_key_value):
                persist_doc["api_key_source"] = "keyvault"
            else:
                persist_doc["api_key"] = api_key_value  # Fallback: store in CosmosDB
            store.upsert({
                "id": model_id,
                "doc_type": "docgrok_model",
                **persist_doc,
                "model_category": model_category,
                "stored_at": datetime.utcnow().isoformat(),
            })
            result = {"id": model_id, "name": model_name, "kind": "external",
                      "model_category": model_category, **persist_doc}
        else:
            # Send full payload (including api_key) to DocGrok for in-memory use
            resp = await http_client.post(f"{DOCGROK_URL}/admin/models/registry", json=reg_payload)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            result = resp.json()

            # Persist to CosmosDB (without api_key) for durability
            model_id = result.get("id", "")
            if model_id.startswith("mdl-ext-"):
                persist_doc = {k: v for k, v in reg_payload.items() if k != "api_key"}
                if api_key_value and set_model_api_key(model_id, api_key_value):
                    persist_doc["api_key_source"] = "keyvault"
                else:
                    persist_doc["api_key"] = api_key_value  # Fallback
                store.upsert({
                    "id": model_id,
                    "doc_type": "docgrok_model",
                    **persist_doc,
                    "model_category": model_category,
                    "stored_at": datetime.utcnow().isoformat(),
                })
            result["model_category"] = model_category

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.put("/api/models/{model_id}")
async def update_model(model_id: str, payload: dict):
    """Update an external model's config (API key, endpoint, etc.)."""
    if not model_id.startswith("mdl-ext-"):
        raise HTTPException(status_code=400, detail="Only external models can be updated")

    store = get_store()
    doc = None
    try:
        doc = store.get(model_id, "docgrok_model")
    except Exception:
        pass
    if not doc:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    # Merge updatable fields
    updatable = ("api_key", "endpoint", "deployment", "api_version", "embedding_dim", "auth_type", "client_id")
    changed = []
    new_api_key = None
    for field in updatable:
        if field in payload and payload[field] is not None:
            val = payload[field].strip() if isinstance(payload[field], str) else payload[field]
            if field == "api_key":
                new_api_key = val
            else:
                doc[field] = val
            changed.append(field)

    if not changed:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    # Handle API key update — Key Vault if available, else CosmosDB
    if new_api_key:
        from keyvault_client import set_model_api_key
        if set_model_api_key(model_id, new_api_key):
            doc["api_key_source"] = "keyvault"
            doc.pop("api_key", None)
        else:
            doc["api_key"] = new_api_key
            doc.pop("api_key_source", None)

    doc["updated_at"] = datetime.utcnow().isoformat()
    store.upsert(doc)

    # Re-register with DocGrok if it's an embedding model (so DocGrok picks up new key/endpoint)
    if doc.get("model_category") != "chat":
        try:
            reg_payload = {
                "id": model_id,
                "name": doc.get("name", ""),
                "type": doc.get("type", "azure-openai"),
                "endpoint": doc.get("endpoint", ""),
                "auth_type": doc.get("auth_type", "key"),
                "api_key": new_api_key or doc.get("api_key", ""),
                "deployment": doc.get("deployment", ""),
                "embedding_dim": int(doc.get("embedding_dim", 1536)),
                "api_version": doc.get("api_version", "2024-06-01"),
            }
            # If key was in Key Vault, read it for DocGrok registration
            if not reg_payload["api_key"] and doc.get("api_key_source") == "keyvault":
                from keyvault_client import get_model_api_key
                reg_payload["api_key"] = get_model_api_key(model_id) or ""
            resp = await http_client.post(f"{DOCGROK_URL}/admin/models/registry", json=reg_payload)
            if resp.status_code >= 400:
                logger.warning(f"DocGrok re-register failed: {resp.text}")
        except Exception as e:
            logger.warning(f"DocGrok re-register error: {e}")

    return {"status": "updated", "id": model_id, "fields_updated": changed}


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    """Delete an external model — proxied to DocGrok, removed from CosmosDB."""
    try:
        store = get_store()

        # Guard: refuse delete if any pipeline or assistant references this model.
        # Pipelines reference models via the `docgrok_pipeline` field (which can
        # also be a transform pipeline id; direct equality is fine as a match).
        # Assistants reference chat models via `model_id`.
        pipeline_users: list[str] = []
        try:
            for d in store.list("pipeline"):
                if d.get("docgrok_pipeline") == model_id:
                    pipeline_users.append(d.get("name") or d.get("id") or "<unnamed>")
        except Exception:
            pass

        assistant_users: list[str] = []
        try:
            for d in store.list("assistant"):
                if d.get("model_id") == model_id:
                    assistant_users.append(d.get("name") or d.get("id") or "<unnamed>")
        except Exception:
            pass

        if pipeline_users or assistant_users:
            parts: list[str] = []
            if pipeline_users:
                names = ", ".join(f"'{n}'" for n in pipeline_users)
                parts.append(f"{len(pipeline_users)} pipeline(s): {names}")
            if assistant_users:
                names = ", ".join(f"'{n}'" for n in assistant_users)
                parts.append(f"{len(assistant_users)} assistant(s): {names}")
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete — model is used by {' and '.join(parts)}. "
                       f"Delete or update those resources first.",
            )

        # Check if it's a chat-only model (only in CosmosDB, not in DocGrok)
        doc = None
        if model_id.startswith("mdl-ext-"):
            try:
                doc = store.get(model_id, "docgrok_model")
            except Exception:
                pass

        if doc and doc.get("model_category") == "chat":
            # Chat model — only delete from CosmosDB
            store.delete(model_id, "docgrok_model")
            return {"status": "deleted", "id": model_id}

        resp = await http_client.delete(f"{DOCGROK_URL}/admin/models/registry/{model_id}")
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        # Remove from CosmosDB persistence and Key Vault
        if model_id.startswith("mdl-ext-"):
            try:
                store.delete(model_id, "docgrok_model")
            except Exception:
                pass
            try:
                from keyvault_client import delete_model_api_key
                delete_model_api_key(model_id)
            except Exception:
                pass

        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/models/{model_id}/test")
async def test_model_health(model_id: str):
    """Test a model's connectivity and auth by making a small embed call."""
    from health_checker import run_health_checks
    result = await run_health_checks(section="models")
    for m in result.get("models", []):
        if m["id"] == model_id:
            return m
    return {"id": model_id, "status": "unknown", "checks": [], "detail": "Model not found in health results"}


# Native model actions (proxy to DocGrok K8s management)
@app.post("/api/models/{model_id}/enable")
async def enable_model(model_id: str):
    name = model_id.replace("mdl-native-", "").replace("native:", "")
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/enable")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/models/{model_id}/disable")
async def disable_model(model_id: str):
    name = model_id.replace("mdl-native-", "").replace("native:", "")
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/disable")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/models/{model_id}/restart")
async def restart_model(model_id: str):
    name = model_id.replace("mdl-native-", "").replace("native:", "")
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/restart")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# =============================================================================
# ASSISTANTS
# =============================================================================

def _assistant_from_doc(doc: dict) -> Assistant:
    return Assistant(
        id=doc["id"],
        name=doc.get("name", ""),
        description=doc.get("description", ""),
        model_id=doc.get("model_id", ""),
        destination_ids=doc.get("destination_ids", []),
        system_prompt=doc.get("system_prompt", ""),
        top_k=doc.get("top_k", 5),
        temperature=doc.get("temperature", 0.7),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


@app.get("/api/assistants")
async def list_assistants():
    store = get_store()
    docs = await asyncio.to_thread(store.list, "assistant")
    assistants = [_assistant_from_doc(d) for d in docs]
    return {"assistants": [a.model_dump() for a in assistants]}


@app.get("/api/assistants/{assistant_id}")
async def get_assistant(assistant_id: str):
    store = get_store()
    doc = await asyncio.to_thread(store.get, assistant_id, "assistant")
    if not doc:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return _assistant_from_doc(doc).model_dump()


@app.post("/api/assistants")
async def create_assistant(req: CreateAssistantRequest):
    store = get_store()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Assistant name cannot be blank")
    # Validate destinations exist
    for did in req.destination_ids:
        if not await asyncio.to_thread(store.get, did, "destination"):
            raise HTTPException(status_code=400, detail=f"Destination '{did}' not found")

    assistant_id = f"ast-{str(uuid.uuid4())[:8]}"
    now = datetime.utcnow()
    assistant = Assistant(
        id=assistant_id,
        name=req.name.strip(),
        description=req.description,
        model_id=req.model_id,
        destination_ids=req.destination_ids,
        system_prompt=req.system_prompt,
        top_k=req.top_k,
        temperature=req.temperature,
        created_at=now,
        updated_at=now,
    )
    doc = assistant.model_dump()
    doc["doc_type"] = "assistant"
    if doc.get("created_at"):
        doc["created_at"] = doc["created_at"].isoformat()
    if doc.get("updated_at"):
        doc["updated_at"] = doc["updated_at"].isoformat()
    await asyncio.to_thread(store.upsert, doc)
    return {"success": True, "assistant": assistant.model_dump()}


@app.put("/api/assistants/{assistant_id}")
async def update_assistant(assistant_id: str, payload: dict):
    store = get_store()
    doc = await asyncio.to_thread(store.get, assistant_id, "assistant")
    if not doc:
        raise HTTPException(status_code=404, detail="Assistant not found")
    for key in ("name", "description", "model_id", "destination_ids", "system_prompt", "top_k", "temperature"):
        if key in payload:
            doc[key] = payload[key]
    doc["updated_at"] = datetime.utcnow().isoformat()
    await asyncio.to_thread(store.upsert, doc)
    return {"success": True, "assistant": _assistant_from_doc(doc).model_dump()}


@app.delete("/api/assistants/{assistant_id}")
async def delete_assistant(assistant_id: str):
    store = get_store()
    doc = await asyncio.to_thread(store.get, assistant_id, "assistant")
    if not doc:
        raise HTTPException(status_code=404, detail="Assistant not found")
    await asyncio.to_thread(store.delete, "assistant", assistant_id)
    return {"success": True}


@app.post("/api/assistants/{assistant_id}/chat")
async def assistant_chat(assistant_id: str, req: AssistantChatRequest):
    """RAG chat: search relevant docs, then call chat model with context."""
    import time
    store = get_store()
    doc = await asyncio.to_thread(store.get, assistant_id, "assistant")
    if not doc:
        raise HTTPException(status_code=404, detail="Assistant not found")
    assistant = _assistant_from_doc(doc)

    # Get the chat model config from CosmosDB
    model_doc = await asyncio.to_thread(store.get, assistant.model_id, "docgrok_model")
    if not model_doc:
        raise HTTPException(status_code=400, detail=f"Chat model '{assistant.model_id}' not found")

    # Step 1: Find an embedding model from the pipeline for each destination
    search_results = []
    if assistant.destination_ids:
        pipeline_docs = await asyncio.to_thread(store.list, "pipeline")
        for dest_id in assistant.destination_ids:
            dest_doc = await asyncio.to_thread(store.get, dest_id, "destination")
            if not dest_doc:
                continue
            destination = _destination_from_doc(dest_doc)
            # Find pipeline with embedding model for this destination
            matched_pip = None
            for pd in pipeline_docs:
                if pd.get("destination_id") == dest_id:
                    matched_pip = pd
                    break
            if not matched_pip:
                continue
            model_ref = matched_pip.get("docgrok_pipeline")
            if not model_ref:
                continue
            # Generate embedding for the user query
            try:
                if model_ref.startswith("mdl-"):
                    embed_body = {"model_id": model_ref, "text": req.message, "requestId": f"ast-{int(time.time())}"}
                else:
                    embed_body = {"pipeline": model_ref, "text": req.message, "requestId": f"ast-{int(time.time())}"}
                resp = await http_client.post(f"{DOCGROK_URL}/embed", json=embed_body, timeout=30.0)
                if resp.status_code != 200:
                    continue
                embed_result = resp.json()
                query_embedding = (embed_result.get("pages") or embed_result.get("output") or [[]])[0]
                if not query_embedding:
                    continue
                # Resolve content_fields from pipeline source
                cf = ["content"]
                sources = matched_pip.get("sources", [])
                if sources:
                    src = sources[0] if isinstance(sources[0], dict) else sources[0]
                    cf = src.get("content_fields", ["content"]) if isinstance(src, dict) else getattr(src, "content_fields", ["content"])
                # Use pipeline's vector_index_path for search
                vf = matched_pip.get("vector_index_path", "").lstrip("/") or None
                results = await asyncio.to_thread(
                    _search_single_index, destination, query_embedding, assistant.top_k, cf, vf
                )
                search_results.extend(results)
            except Exception as e:
                logger.warning(f"Assistant search failed for {dest_id}: {e}")

    # Step 2: Build context from search results
    context_parts = []
    for r in search_results[:assistant.top_k]:
        text = r.get("text", "")
        if text:
            context_parts.append(text)
    context = "\n\n---\n\n".join(context_parts)

    # Step 3: Call the chat model
    model_type = model_doc.get("type", "azure-openai")
    endpoint = model_doc.get("endpoint", "")
    # Retrieve API key from Key Vault first, fall back to CosmosDB doc
    from keyvault_client import get_model_api_key
    api_key = get_model_api_key(model_doc.get("id", "")) or model_doc.get("api_key", "")
    deployment = model_doc.get("deployment", "")
    api_version = model_doc.get("api_version", "2024-06-01")

    # Build messages
    messages = []
    system_content = assistant.system_prompt or "You are a helpful assistant."
    if context:
        system_content += f"\n\nUse the following context to answer the user's question. If the context doesn't contain relevant information, say so.\n\n{context}"
    messages.append({"role": "system", "content": system_content})

    # Add conversation history
    for msg in req.conversation:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    # Add current message
    messages.append({"role": "user", "content": req.message})

    # Call the appropriate API
    try:
        if model_type in ("azure-openai",):
            chat_url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
            headers = {"api-key": api_key, "Content-Type": "application/json"}
        else:
            # OpenAI-compatible
            chat_url = f"{endpoint.rstrip('/')}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        chat_body = {
            "messages": messages,
            "temperature": assistant.temperature,
            "max_tokens": 2000,
        }
        if model_type not in ("azure-openai",):
            chat_body["model"] = deployment

        chat_resp = await http_client.post(chat_url, json=chat_body, headers=headers, timeout=60.0)
        if chat_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Chat model error: {chat_resp.text[:200]}")

        chat_data = chat_resp.json()
        reply = chat_data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = chat_data.get("usage", {})

        return {
            "reply": reply,
            "sources": [{"id": r.get("id"), "text": r.get("text", "")[:200], "score": r.get("score", 0)} for r in search_results[:assistant.top_k]],
            "usage": usage,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chat model error: {str(e)}")


# =============================================================================
# DOCGROK PROXY
# =============================================================================

@app.get("/api/docgrok/pipelines")
async def get_docgrok_pipelines():
    """Get available DocGrok pipelines."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/pipelines")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/pipelines/options")
async def get_docgrok_pipeline_options():
    """Get DocGrok pipeline options (local functions, models, external providers)."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/pipelines/options")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/pipelines/{name}")
async def get_docgrok_pipeline(name: str):
    """Get a specific DocGrok pipeline."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/pipelines/{name}")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found")
        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/pipelines")
async def create_docgrok_pipeline(payload: dict):
    """Create a new DocGrok pipeline (e.g. transform pipeline with worker_url)."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/pipelines", json=payload)
        result = resp.json()
        # Persist to CosmosDB
        try:
            name = payload.get("name", "")
            store = get_store()
            doc = {**payload, "id": name, "doc_type": "docgrok_pipeline", "stored_at": datetime.utcnow().isoformat()}
            await asyncio.to_thread(store.upsert, doc)
            logger.info("Created DocGrok pipeline '%s' in metadata store", name)
        except Exception as pe:
            logger.warning("Failed to persist DocGrok pipeline: %s", pe)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.put("/api/docgrok/pipelines/{name}")
async def update_docgrok_pipeline(name: str, payload: dict):
    """Update a DocGrok pipeline."""
    try:
        resp = await http_client.put(f"{DOCGROK_URL}/admin/pipelines/{name}", json=payload)
        result = resp.json()
        # Persist to CosmosDB (non-blocking)
        try:
            store = get_store()
            doc = {**payload, "id": name, "doc_type": "docgrok_pipeline", "stored_at": datetime.utcnow().isoformat()}
            await asyncio.to_thread(store.upsert, doc)
            logger.info("Updated DocGrok pipeline '%s' in metadata store", name)
        except Exception as pe:
            logger.warning("Failed to persist DocGrok pipeline update: %s", pe)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.delete("/api/docgrok/pipelines/{name}")
async def delete_docgrok_pipeline(name: str):
    """Delete a DocGrok pipeline."""
    try:
        resp = await http_client.delete(f"{DOCGROK_URL}/admin/pipelines/{name}")
        result = resp.json()
        # Remove from CosmosDB (non-blocking)
        try:
            store = get_store()
            await asyncio.to_thread(store.delete, "docgrok_pipeline", name)
            logger.info("Deleted DocGrok pipeline '%s' from metadata store", name)
        except Exception as pe:
            logger.warning("Failed to delete DocGrok pipeline from store: %s", pe)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/health")
async def get_docgrok_health():
    """Get DocGrok health status."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/health")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/models")
async def get_docgrok_models():
    """Get DocGrok models (K8s deployments)."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/models")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/models/{name}/enable")
async def enable_docgrok_model(name: str):
    """Enable/start a DocGrok model."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/enable")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/models/{name}/disable")
async def disable_docgrok_model(name: str):
    """Disable/stop a DocGrok model."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/disable")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/models/{name}/restart")
async def restart_docgrok_model(name: str):
    """Restart a DocGrok model."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/restart")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/models/{name}/scale")
async def scale_docgrok_model(name: str, payload: dict):
    """Scale a DocGrok model."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/models/{name}/scale", json=payload)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")




@app.get("/api/docgrok/logs/{name}")
async def get_docgrok_logs(name: str, lines: int = 100):
    """Get logs for a DocGrok model."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/logs/{name}?lines={lines}")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/system")
async def get_docgrok_system():
    """Get DocGrok system information."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/system")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/deployments")
async def get_docgrok_deployments():
    """Get DocGrok-related K8s deployments (router, controller, models)."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/deployments")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.get("/api/docgrok/health/models")
async def get_docgrok_model_health():
    """Get model health from DocGrok controller (proxied through router)."""
    try:
        resp = await http_client.get(f"{DOCGROK_URL}/admin/health/models")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/deployments/{name}/scale")
async def scale_docgrok_deployment(name: str, request: Request):
    """Proxy scale request to DocGrok router."""
    try:
        body = await request.json()
        resp = await http_client.post(
            f"{DOCGROK_URL}/admin/deployments/{name}/scale",
            json=body,
        )
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


@app.post("/api/docgrok/deployments/{name}/restart")
async def restart_docgrok_deployment(name: str):
    """Proxy restart request to DocGrok router."""
    try:
        resp = await http_client.post(f"{DOCGROK_URL}/admin/deployments/{name}/restart")
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DocGrok error: {str(e)}")


# =============================================================================
# DOCGROK PERSISTENCE & SYNC
# =============================================================================

@app.get("/api/docgrok/stored/pipelines")
async def get_stored_docgrok_pipelines():
    """Get DocGrok pipelines stored in CosmosDB."""
    store = get_store()
    docs = await asyncio.to_thread(store.list, "docgrok_pipeline")
    return {"pipelines": [{k: v for k, v in d.items() if not k.startswith("_")} for d in docs]}




# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_job_stats() -> JobStats:
    """Calculate job statistics using aggregate query (efficient for large job counts)."""
    store = get_store()
    stats = JobStats()
    query = "SELECT c.status, COUNT(1) AS cnt FROM c WHERE c.doc_type = 'job' GROUP BY c.status"
    try:
        rows = store.query(query, partition_key="job")
        for row in rows:
            status = row.get("status", "").upper()
            cnt = row.get("cnt", 0)
            stats.total += cnt
            if status == "PENDING":
                stats.pending = cnt
            elif status == "PROCESSING":
                stats.processing = cnt
            elif status == "COMPLETED":
                stats.completed = cnt
            elif status == "FAILED":
                stats.failed = cnt
    except Exception:
        # Fallback: just count total
        query = "SELECT VALUE COUNT(1) FROM c WHERE c.doc_type = 'job'"
        try:
            result = store.query(query, partition_key="job")
            stats.total = result[0] if result else 0
        except Exception:
            pass
    return stats


_pg_stats_cache: dict = {}  # key: (source_id, dest_table) -> (source_count, embed_count, timestamp)

def _get_pg_stats_cached(source, pipeline, store):
    """Get PG source/embed counts with 30s cache to avoid connection exhaustion."""
    import asyncio, time
    from health_checker import _connect_pg

    config = source.config
    table = config.get("table", "")
    schema = config.get("schema_name", config.get("schema", "public"))

    # Determine embed table (may differ for queue mode)
    embed_table, embed_schema, embed_config = table, schema, config
    if pipeline.processing_mode != "inline" and pipeline.destination_id:
        dest_doc = store.get(pipeline.destination_id, "destination")
        if dest_doc:
            dest_cfg = dest_doc.get("config", {})
            embed_table = dest_cfg.get("table", table)
            embed_schema = dest_cfg.get("schema_name", dest_cfg.get("schema", "public"))
            embed_config = dest_cfg

    cfp_gen = _cfp_generation(pipeline.reset_at)
    cache_key = f"{source.id}:{embed_schema}.{embed_table}:{cfp_gen}"
    cached = _pg_stats_cache.get(cache_key)
    if cached and time.time() - cached[2] < 30:
        return (cached[0], cached[1])

    try:
        async def _query():
            conn = await _connect_pg(config)
            try:
                src_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
                embed_conn = conn
                if embed_config is not config:
                    embed_conn = await _connect_pg(embed_config)
                try:
                    try:
                        embed_count = await embed_conn.fetchval(
                            f'SELECT COUNT(*) FROM "{embed_schema}"."{embed_table}" WHERE embedding IS NOT NULL AND cfp_generation = $1',
                            cfp_gen)
                    except Exception:
                        # cfp_generation column may not exist yet
                        embed_count = await embed_conn.fetchval(
                            f'SELECT COUNT(*) FROM "{embed_schema}"."{embed_table}" WHERE embedding IS NOT NULL')
                finally:
                    if embed_conn is not conn:
                        await embed_conn.close()
                return (src_count, embed_count)
            finally:
                await conn.close()

        result = asyncio.run(_query())
        _pg_stats_cache[cache_key] = (result[0], result[1], time.time())
        return result
    except Exception as e:
        logger.warning(f"PG stats query failed: {e}")
        if cached:
            return (cached[0], cached[1])  # return stale cache
        return None


def _get_blob_embed_count_pg(dest_cfg, pipeline_id, reset_at):
    """Count rows in a pgvector destination table for a given pipeline since reset_at.

    Used when source.type == AZURE_BLOB and destination.type == pgvector.
    Returns int (row count) or None on failure. Resilient to missing
    cfp_generation / embedded_at columns on older tables.
    """
    import asyncio
    from health_checker import _connect_pg

    table = dest_cfg.get("table", "")
    schema = dest_cfg.get("schema_name", dest_cfg.get("schema", "public"))
    if not table:
        return None

    async def _q():
        conn = await _connect_pg(dest_cfg)
        try:
            # Prefer pipeline_id + embedded_at filter; fall back gracefully.
            try:
                return await conn.fetchval(
                    f'SELECT COUNT(*) FROM "{schema}"."{table}" '
                    f'WHERE pipeline_id = $1 AND embedded_at >= $2::timestamptz',
                    pipeline_id, reset_at)
            except Exception:
                try:
                    return await conn.fetchval(
                        f'SELECT COUNT(*) FROM "{schema}"."{table}" WHERE pipeline_id = $1',
                        pipeline_id)
                except Exception:
                    return await conn.fetchval(
                        f'SELECT COUNT(*) FROM "{schema}"."{table}" WHERE embedding IS NOT NULL')
        finally:
            await conn.close()

    try:
        return asyncio.run(_q())
    except Exception as e:
        logger.warning(f"pgvector blob embed count failed: {e}")
        return None


def _get_blob_embed_count_mssql(dest_cfg, pipeline_id, reset_at):
    """Count rows in an MSSQL destination table for a given pipeline since reset_at."""
    try:
        import pyodbc
    except ImportError:
        logger.warning("pyodbc not installed; cannot count mssql blob embeddings")
        return None

    host = dest_cfg.get("host", "")
    database = dest_cfg.get("database", "")
    user = dest_cfg.get("user", "")
    password = dest_cfg.get("password", "")
    port = dest_cfg.get("port", 1433)
    table = dest_cfg.get("table", "")
    schema = dest_cfg.get("schema_name", dest_cfg.get("schema", "dbo"))
    if not (host and database and table):
        return None

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE={database};UID={user};PWD={password};Encrypt=yes;TrustServerCertificate=yes;"
    )
    try:
        cn = pyodbc.connect(conn_str, timeout=5)
        cur = cn.cursor()
        try:
            cur.execute(
                f"SELECT COUNT(*) FROM [{schema}].[{table}] "
                f"WHERE pipeline_id = ? AND embedded_at >= ?",
                pipeline_id, reset_at)
            return cur.fetchone()[0]
        except Exception:
            cur.execute(
                f"SELECT COUNT(*) FROM [{schema}].[{table}] WHERE pipeline_id = ?",
                pipeline_id)
            return cur.fetchone()[0]
        finally:
            cur.close()
            cn.close()
    except Exception as e:
        logger.warning(f"mssql blob embed count failed: {e}")
        return None


_mssql_stats_cache: dict = {}  # key: (source_id, dest_table) -> (source_count, embed_count, timestamp)

def _get_mssql_stats_cached(source, pipeline, store):
    """Get MSSQL source/embed counts with 30s cache to avoid connection exhaustion."""
    import time

    config = source.config
    table = config.get("table", "")
    schema = config.get("schema_name", config.get("schema", "dbo"))

    # Determine embed table (may differ for queue mode)
    embed_table, embed_schema, embed_config = table, schema, config
    if pipeline.processing_mode != "inline" and pipeline.destination_id:
        dest_doc = store.get(pipeline.destination_id, "destination")
        if dest_doc:
            dest_cfg = dest_doc.get("config", {})
            embed_table = dest_cfg.get("table", table)
            embed_schema = dest_cfg.get("schema_name", dest_cfg.get("schema", "dbo"))
            embed_config = dest_cfg

    cfp_gen = _cfp_generation(pipeline.reset_at)
    cache_key = f"mssql:{source.id}:{embed_schema}.{embed_table}:{cfp_gen}"
    cached = _mssql_stats_cache.get(cache_key)
    if cached and time.time() - cached[2] < 30:
        return (cached[0], cached[1])

    try:
        import pyodbc

        def _build_mssql_conn_str(cfg):
            cs = cfg.get("connection_string", "")
            if cs:
                # Convert ADO.NET format to ODBC if needed
                if "Driver=" not in cs and "DRIVER=" not in cs:
                    parts = {}
                    for part in cs.split(";"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            parts[k.strip().lower()] = v.strip()
                    server = parts.get("server", parts.get("host", parts.get("data source", "")))
                    database = parts.get("database", parts.get("initial catalog", ""))
                    user = parts.get("username", parts.get("user id", parts.get("uid", "")))
                    password = parts.get("password", parts.get("pwd", ""))
                    cs = f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Uid={user};Pwd={password};Encrypt=yes;TrustServerCertificate=yes;"
                return cs
            server = cfg.get("server", cfg.get("host", ""))
            database = cfg.get("database", "")
            user = cfg.get("user", cfg.get("username", ""))
            password = cfg.get("password", "")
            return f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Uid={user};Pwd={password};Encrypt=yes;TrustServerCertificate=yes;"

        conn_str = _build_mssql_conn_str(config)
        conn = pyodbc.connect(conn_str, timeout=10)
        try:
            cursor = conn.cursor()
            src_count = cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]").fetchone()[0]

            # Get embed count
            embed_conn = conn
            embed_conn_str = conn_str
            if embed_config is not config:
                embed_conn_str = _build_mssql_conn_str(embed_config)
                embed_conn = pyodbc.connect(embed_conn_str, timeout=10)

            try:
                cursor2 = embed_conn.cursor()
                try:
                    embed_count = cursor2.execute(
                        f"SELECT COUNT(*) FROM [{embed_schema}].[{embed_table}] WHERE embedding IS NOT NULL AND cfp_generation = ?",
                        cfp_gen).fetchone()[0]
                except Exception:
                    embed_count = cursor2.execute(
                        f"SELECT COUNT(*) FROM [{embed_schema}].[{embed_table}] WHERE embedding IS NOT NULL").fetchone()[0]
            finally:
                if embed_conn is not conn:
                    embed_conn.close()
        finally:
            conn.close()

        _mssql_stats_cache[cache_key] = (src_count, embed_count, time.time())
        return (src_count, embed_count)
    except Exception as e:
        logger.warning(f"MSSQL stats query failed: {e}")
        if cached:
            return (cached[0], cached[1])
        return None


def get_pipeline_stats(pipeline_id: str) -> PipelineRunStats:
    """Get statistics for a specific pipeline using aggregate queries."""
    store = get_store()
    doc = store.get(pipeline_id, "pipeline")
    if not doc:
        return PipelineRunStats(pipeline_id=pipeline_id, pipeline_name="", jobs=JobStats())

    pipeline = _pipeline_from_doc(doc)
    jobs = JobStats()

    # Use aggregate query instead of fetching all 76K+ job docs
    query = (
        "SELECT c.status, COUNT(1) AS cnt FROM c "
        "WHERE c.doc_type = 'job' AND c.pipeline_id = @pid "
        "GROUP BY c.status"
    )
    params = [{"name": "@pid", "value": pipeline_id}]
    try:
        rows = store.query(query, params, partition_key="job")
        for row in rows:
            status = row.get("status", "").upper()
            cnt = row.get("cnt", 0)
            jobs.total += cnt
            if status == "PENDING":
                jobs.pending = cnt
            elif status == "PROCESSING":
                jobs.processing = cnt
            elif status == "COMPLETED":
                jobs.completed = cnt
            elif status == "FAILED":
                jobs.failed = cnt
    except Exception:
        pass

    # Compute throughput: overall and recent (last 60s)
    throughput = None
    recent_throughput = None
    avg_time = None
    try:
        # Overall: time span from first started to last completed
        span_query = (
            "SELECT MIN(c.started_at) AS first_start, MAX(c.completed_at) AS last_complete, "
            "AVG(c.result.processing_time_ms) AS avg_ms "
            "FROM c WHERE c.doc_type = 'job' AND c.pipeline_id = @pid AND c.status = 'completed'"
        )
        span_rows = store.query(span_query, params, partition_key="job")
        for row in span_rows:
            fs = row.get("first_start")
            lc = row.get("last_complete")
            avg_ms = row.get("avg_ms")
            if fs and lc and jobs.completed > 0:
                from datetime import datetime as dt
                t0 = dt.fromisoformat(fs.replace("Z", "+00:00")) if isinstance(fs, str) else fs
                t1 = dt.fromisoformat(lc.replace("Z", "+00:00")) if isinstance(lc, str) else lc
                span_sec = (t1 - t0).total_seconds()
                if span_sec > 0:
                    throughput = round(jobs.completed / span_sec, 1)
            if avg_ms is not None:
                avg_time = round(avg_ms, 1)
    except Exception:
        pass

    try:
        # Recent: jobs completed in last 60s
        cutoff = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
        recent_query = (
            "SELECT COUNT(1) AS cnt FROM c "
            "WHERE c.doc_type = 'job' AND c.pipeline_id = @pid "
            "AND c.status = 'completed' AND c.completed_at >= @cutoff"
        )
        recent_params = [{"name": "@pid", "value": pipeline_id}, {"name": "@cutoff", "value": cutoff}]
        recent_rows = store.query(recent_query, recent_params, partition_key="job")
        for row in recent_rows:
            cnt = row.get("cnt", 0)
            if cnt > 0:
                recent_throughput = round(cnt / 60.0, 1)
    except Exception:
        pass

    # Ground truth: total docs from source container, embedded count from where embeddings land
    # Inline mode patches source docs in-place; queue mode writes to destination container
    docs_processed = jobs.completed
    source_doc_count = None
    embedded_count = 0
    completion_pct = None

    try:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        source_id = pipeline.sources[0].source_id if pipeline.sources else None
        if source_id:
            source_doc = store.get(source_id, "source")
            if source_doc:
                source = _source_from_doc(source_doc, mask=False)
                if source.type == SourceType.COSMOSDB:
                    src_endpoint = source.config.get("endpoint", "")
                    src_client = CosmosClient(src_endpoint, credential=DefaultAzureCredential())
                    src_container = src_client.get_database_client(source.config["database"]).get_container_client(source.config["container"])

                    # Total docs in source
                    total_query = "SELECT VALUE COUNT(1) FROM c"
                    total_result = list(src_container.query_items(total_query, enable_cross_partition_query=True))
                    if total_result:
                        source_doc_count = total_result[0]

                    # Embedded count: query the container where embeddings are written
                    # For inline mode â†’ source container; for queue mode â†’ destination container
                    embed_container = src_container  # default: same as source (inline)
                    if pipeline.processing_mode != "inline" and pipeline.destination_id:
                        dest_doc = store.get(pipeline.destination_id, "destination")
                        if dest_doc:
                            dest_cfg = dest_doc.get("config", {})
                            dest_endpoint = dest_cfg.get("endpoint", "")
                            if dest_endpoint:
                                dest_client = CosmosClient(dest_endpoint, credential=DefaultAzureCredential())
                                embed_container = dest_client.get_database_client(dest_cfg["database"]).get_container_client(dest_cfg["container"])

                    reset_at = pipeline.reset_at or "1970-01-01T00:00:00"
                    # Ensure reset_at is a string (CosmosDB SDK may parse it as datetime)
                    if hasattr(reset_at, 'isoformat'):
                        reset_at = reset_at.isoformat()
                    reset_at = str(reset_at)
                    count_query = (
                        "SELECT VALUE COUNT(1) FROM c "
                        "WHERE c.pipeline_id = @pid AND c.embedded_at >= @reset_at"
                    )
                    count_params = [
                        {"name": "@pid", "value": pipeline_id},
                        {"name": "@reset_at", "value": reset_at},
                    ]
                    result = list(embed_container.query_items(count_query, parameters=count_params, enable_cross_partition_query=True))
                    if result:
                        embedded_count = result[0]

                    docs_processed = embedded_count
                    if source_doc_count and source_doc_count > 0:
                        completion_pct = round(embedded_count / source_doc_count * 100, 1)

                elif source.type == SourceType.POSTGRESQL:
                    cached = _get_pg_stats_cached(source, pipeline, store)
                    if cached:
                        source_doc_count = cached[0]
                        embedded_count = cached[1]
                        docs_processed = embedded_count
                        if source_doc_count and source_doc_count > 0:
                            completion_pct = round(embedded_count / source_doc_count * 100, 1)

                elif source.type == SourceType.MSSQL:
                    cached = _get_mssql_stats_cached(source, pipeline, store)
                    if cached:
                        source_doc_count = cached[0]
                        embedded_count = cached[1]
                        docs_processed = embedded_count
                        if source_doc_count and source_doc_count > 0:
                            completion_pct = round(embedded_count / source_doc_count * 100, 1)

                elif source.type == SourceType.AZURE_BLOB:
                    # Count blobs in source container
                    from azure.storage.blob import BlobServiceClient
                    conn_str = source.config.get("connection_string", "")
                    acct_url = source.config.get("account_url", "")
                    container_name = source.config.get("container", "")
                    prefix = source.config.get("prefix", "")
                    file_type = source.config.get("file_type", "pdf")

                    if container_name and (conn_str or acct_url):
                        if conn_str:
                            blob_client = BlobServiceClient.from_connection_string(conn_str)
                        else:
                            blob_client = BlobServiceClient(acct_url, credential=DefaultAzureCredential())
                        container_client = blob_client.get_container_client(container_name)
                        blob_count = 0
                        for blob in container_client.list_blobs(name_starts_with=prefix or None):
                            if file_type and not blob.name.lower().endswith(f".{file_type}"):
                                continue
                            blob_count += 1
                        source_doc_count = blob_count

                    # Count embedded docs in the pipeline's destination store.
                    # Dispatch by destination type: cosmosdb | pgvector | mssql.
                    if pipeline.destination_id:
                        dest_doc = store.get(pipeline.destination_id, "destination")
                        if dest_doc:
                            dest_cfg = dest_doc.get("config", {})
                            dest_type = (dest_doc.get("type") or "").lower()
                            reset_at = pipeline.reset_at or "1970-01-01T00:00:00"
                            if hasattr(reset_at, 'isoformat'):
                                reset_at = reset_at.isoformat()
                            reset_at = str(reset_at)

                            try:
                                if dest_type in ("cosmosdb", "cosmos", "azure-cosmos-db"):
                                    dest_endpoint = dest_cfg.get("endpoint", "")
                                    if dest_endpoint:
                                        dest_client = CosmosClient(dest_endpoint, credential=DefaultAzureCredential())
                                        embed_container = dest_client.get_database_client(dest_cfg["database"]).get_container_client(dest_cfg["container"])
                                        count_query = (
                                            "SELECT VALUE COUNT(1) FROM c "
                                            "WHERE c.pipeline_id = @pid AND c.embedded_at >= @reset_at"
                                        )
                                        count_params = [
                                            {"name": "@pid", "value": pipeline_id},
                                            {"name": "@reset_at", "value": reset_at},
                                        ]
                                        result = list(embed_container.query_items(count_query, parameters=count_params, enable_cross_partition_query=True))
                                        if result:
                                            embedded_count = result[0]
                                elif dest_type in ("pgvector", "postgresql", "postgres"):
                                    ec = _get_blob_embed_count_pg(dest_cfg, pipeline_id, reset_at)
                                    if ec is not None:
                                        embedded_count = ec
                                elif dest_type in ("mssql", "sqlserver", "sql-server"):
                                    ec = _get_blob_embed_count_mssql(dest_cfg, pipeline_id, reset_at)
                                    if ec is not None:
                                        embedded_count = ec
                            except Exception as ce:
                                logger.warning(f"Blob embedded_count query failed (dest_type={dest_type}): {ce}")

                    docs_processed = embedded_count
                    if source_doc_count and source_doc_count > 0:
                        completion_pct = round(embedded_count / source_doc_count * 100, 1)

    except Exception as e:
        import traceback
        logger.error(f"Error computing pipeline stats for {pipeline_id}: {e}\n{traceback.format_exc()}")

    return PipelineRunStats(
        pipeline_id=pipeline_id,
        pipeline_name=pipeline.name,
        jobs=jobs,
        documents_processed=docs_processed,
        source_doc_count=source_doc_count,
        embedded_count=embedded_count,
        completion_pct=completion_pct,
        avg_processing_time_ms=avg_time,
        throughput_docs_per_sec=throughput,
        recent_throughput_docs_per_sec=recent_throughput,
    )



# =============================================================================
# OPERATIONAL SETTINGS (persisted in CosmosDB, applied to K8s)
# =============================================================================

CONFIG_DOC_ID = "operational-config"

# Supported settings and how they map to K8s resources
SETTINGS_SCHEMA = {
    "changefeed.replicas":    {"type": int, "min": 1, "max": 30, "deployment": "omnivec-source-connector", "hpa_field": "both"},
    "worker.minReplicas":     {"type": int, "min": 1, "max": 30, "deployment": "omnivec-worker",     "hpa_field": "min"},
    "worker.maxReplicas":     {"type": int, "min": 1, "max": 30, "deployment": "omnivec-worker",     "hpa_field": "max"},
    "controller.replicas":    {"type": int, "min": 1, "max": 3,  "deployment": "omnivec-controller", "hpa_field": None},
    "api.replicas":           {"type": int, "min": 1, "max": 10, "deployment": "omnivec-api",        "hpa_field": None},
    "web.replicas":           {"type": int, "min": 1, "max": 10, "deployment": "omnivec-web",        "hpa_field": None},
}


def _apply_setting_to_k8s(key: str, value, apps_v1, autoscaling_v2):
    """Apply a single setting to the live K8s cluster."""
    schema = SETTINGS_SCHEMA[key]
    dep_name = schema["deployment"]
    hpa_field = schema["hpa_field"]

    if hpa_field in ("both", "min", "max"):
        # Update HPA
        hpa_patch = {}
        if hpa_field == "both":
            hpa_patch = {"spec": {"minReplicas": value, "maxReplicas": value}}
        elif hpa_field == "min":
            hpa_patch = {"spec": {"minReplicas": value}}
        elif hpa_field == "max":
            hpa_patch = {"spec": {"maxReplicas": value}}
        try:
            autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                dep_name, OMNIVEC_NAMESPACE, body=hpa_patch,
            )
        except Exception:
            pass  # HPA may not exist for this deployment

    if hpa_field in ("both", None):
        # Update deployment replicas directly
        apps_v1.patch_namespaced_deployment_scale(
            dep_name, OMNIVEC_NAMESPACE,
            body={"spec": {"replicas": value}},
        )


@app.get("/api/settings")
async def get_settings():
    """Get operational settings (persisted in CosmosDB + live K8s state)."""
    store = get_store()

    # Read saved config
    saved = {}
    try:
        doc = store.get(CONFIG_DOC_ID, "config")
        if doc:
            saved = {k: v for k, v in doc.items()
                     if k in SETTINGS_SCHEMA}
    except Exception:
        pass

    # Read live K8s state
    live = {}
    try:
        apps_v1, _, autoscaling_v2 = _get_k8s_clients()
        hpa_map = {}
        try:
            hpa_list = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(OMNIVEC_NAMESPACE)
            for hpa in hpa_list.items:
                hpa_map[hpa.metadata.name] = hpa
        except Exception:
            pass

        for key, schema in SETTINGS_SCHEMA.items():
            dep_name = schema["deployment"]
            try:
                dep = apps_v1.read_namespaced_deployment(dep_name, OMNIVEC_NAMESPACE)
                hpa = hpa_map.get(dep_name)

                if schema["hpa_field"] == "both" and hpa:
                    live[key] = hpa.spec.min_replicas
                elif schema["hpa_field"] == "min" and hpa:
                    live[key] = hpa.spec.min_replicas
                elif schema["hpa_field"] == "max" and hpa:
                    live[key] = hpa.spec.max_replicas
                else:
                    live[key] = dep.spec.replicas
            except Exception:
                pass
    except Exception:
        pass

    # Build response with schema info
    settings = []
    for key, schema in SETTINGS_SCHEMA.items():
        settings.append({
            "key": key,
            "saved": saved.get(key),
            "live": live.get(key),
            "min": schema["min"],
            "max": schema["max"],
            "drift": saved.get(key) is not None and live.get(key) is not None and saved[key] != live[key],
        })

    return {"settings": settings}


@app.put("/api/settings")
async def update_settings(payload: dict):
    """Update operational settings. Applies to K8s immediately and persists to CosmosDB.

    Body: {"changefeed.replicas": 15, "worker.maxReplicas": 10, ...}
    """
    errors = []
    applied = []

    # Validate all settings first
    for key, value in payload.items():
        if key not in SETTINGS_SCHEMA:
            errors.append(f"Unknown setting: {key}")
            continue
        schema = SETTINGS_SCHEMA[key]
        if not isinstance(value, schema["type"]):
            errors.append(f"{key}: expected {schema['type'].__name__}, got {type(value).__name__}")
            continue
        if value < schema["min"] or value > schema["max"]:
            errors.append(f"{key}: value {value} out of range [{schema['min']}, {schema['max']}]")
            continue

    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    # Apply to K8s
    try:
        apps_v1, _, autoscaling_v2 = _get_k8s_clients()
        for key, value in payload.items():
            _apply_setting_to_k8s(key, value, apps_v1, autoscaling_v2)
            applied.append(key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to apply to K8s: {str(e)}")

    # Persist to CosmosDB
    store = get_store()
    try:
        doc = store.get(CONFIG_DOC_ID, "config")
        if not doc:
            doc = {"id": CONFIG_DOC_ID, "doc_type": "config"}
    except Exception:
        doc = {"id": CONFIG_DOC_ID, "doc_type": "config"}

    for key, value in payload.items():
        doc[key] = value
    doc["updated_at"] = datetime.utcnow().isoformat()
    doc["updated_by"] = "ui"
    store.upsert(doc)

    return {"success": True, "applied": applied}


# =============================================================================
# IMPORT / EXPORT (deployment bundles)
# =============================================================================

_EXPORT_VERSION = "1.0"

# Resource types handled by export/import, with their CosmosDB doc_type
_EXPORT_RESOURCE_TYPES = {
    "sources": "source",
    "destinations": "destination",
    "pipelines": "pipeline",
    "models": "docgrok_model",
    "assistants": "assistant",
}


def _strip_internal(doc: dict) -> dict:
    """Remove Cosmos internal fields (_rid, _etag, _self, ...) and doc_type."""
    return {k: v for k, v in doc.items() if not k.startswith("_") and k != "doc_type"}


def _redact_secrets_in_config(cfg: dict) -> dict:
    """Replace sensitive values inside a config dict with '***'."""
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if any(s in k.lower() for s in _SENSITIVE_CONFIG_KEYS):
            out[k] = "***" if v not in (None, "", 0) else v
        else:
            out[k] = v
    return out


def _redact_model_doc(doc: dict) -> dict:
    """Redact sensitive fields from a docgrok_model doc."""
    out = dict(doc)
    for k in list(out.keys()):
        if any(s in k.lower() for s in _SENSITIVE_CONFIG_KEYS):
            if out[k] not in (None, "", 0):
                out[k] = "***"
    return out


def _collect_pipeline_refs(pipelines: list[dict]) -> tuple[set, set, set]:
    """Return (source_ids, destination_ids, model_refs) referenced by the given pipelines."""
    src_ids, dst_ids, mdl_refs = set(), set(), set()
    for p in pipelines:
        for ps in p.get("sources", []) or []:
            sid = ps.get("source_id") if isinstance(ps, dict) else None
            if sid:
                src_ids.add(sid)
        if p.get("destination_id"):
            dst_ids.add(p["destination_id"])
        if p.get("docgrok_pipeline"):
            mdl_refs.add(p["docgrok_pipeline"])
    return src_ids, dst_ids, mdl_refs


@app.get("/api/admin/export")
def export_bundle(
    include: str = "sources,destinations,pipelines,models,assistants",
    include_secrets: bool = False,
    include_checkpoints: bool = False,
    pipeline_ids: str = "",
    source_ids: str = "",
    destination_ids: str = "",
    model_ids: str = "",
    assistant_ids: str = "",
    download: bool = False,
):
    """Export OmniVec deployment data as a JSON bundle.

    Query params:
      - include: csv of {sources,destinations,pipelines,models,assistants}
      - include_secrets: if true, keep connection strings / api keys / passwords
      - include_checkpoints: if true, append all (or pipeline-scoped) checkpoints
      - pipeline_ids: csv; when set, only export these pipelines plus the sources,
        destinations, models, and assistants they reference (unless overridden by
        an explicit per-type *_ids filter below)
      - source_ids / destination_ids / model_ids / assistant_ids: csv; when set,
        filter that type to exactly those IDs (overrides pipeline-driven auto
        inclusion for that type)
      - download: if true, send as attachment
    """
    store = get_store()
    wanted = {t.strip() for t in include.split(",") if t.strip()}
    unknown = wanted - set(_EXPORT_RESOURCE_TYPES.keys())
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown resource type(s): {sorted(unknown)}")

    def _csv_set(v: str) -> set:
        return {x.strip() for x in (v or "").split(",") if x.strip()}

    pipeline_filter = _csv_set(pipeline_ids)
    src_filter = _csv_set(source_ids)
    dst_filter = _csv_set(destination_ids)
    mdl_filter = _csv_set(model_ids)
    ast_filter = _csv_set(assistant_ids)

    resources: dict[str, list[dict]] = {k: [] for k in _EXPORT_RESOURCE_TYPES}

    # Load pipelines first because filter is pipeline-driven
    all_pipelines = [_strip_internal(d) for d in store.list("pipeline")]
    if pipeline_filter:
        all_pipelines = [p for p in all_pipelines if p.get("id") in pipeline_filter]
        missing = pipeline_filter - {p.get("id") for p in all_pipelines}
        if missing:
            raise HTTPException(status_code=404, detail=f"Pipeline(s) not found: {sorted(missing)}")
    if "pipelines" in wanted:
        resources["pipelines"] = all_pipelines

    ref_src, ref_dst, ref_mdl = _collect_pipeline_refs(all_pipelines)

    if "sources" in wanted:
        docs = [_strip_internal(d) for d in store.list("source")]
        if src_filter:
            docs = [d for d in docs if d.get("id") in src_filter]
        elif pipeline_filter:
            docs = [d for d in docs if d.get("id") in ref_src]
        if not include_secrets:
            for d in docs:
                if isinstance(d.get("config"), dict):
                    d["config"] = _redact_secrets_in_config(d["config"])
        resources["sources"] = docs

    if "destinations" in wanted:
        docs = [_strip_internal(d) for d in store.list("destination")]
        if dst_filter:
            docs = [d for d in docs if d.get("id") in dst_filter]
        elif pipeline_filter:
            docs = [d for d in docs if d.get("id") in ref_dst]
        if not include_secrets:
            for d in docs:
                if isinstance(d.get("config"), dict):
                    d["config"] = _redact_secrets_in_config(d["config"])
        resources["destinations"] = docs

    if "models" in wanted:
        docs = [_strip_internal(d) for d in store.list("docgrok_model")]
        if mdl_filter:
            docs = [d for d in docs if d.get("id") in mdl_filter or d.get("name") in mdl_filter]
        elif pipeline_filter:
            docs = [d for d in docs if d.get("id") in ref_mdl or d.get("name") in ref_mdl]
        if not include_secrets:
            docs = [_redact_model_doc(d) for d in docs]
        resources["models"] = docs

    if "assistants" in wanted:
        docs = [_strip_internal(d) for d in store.list("assistant")]
        if ast_filter:
            docs = [d for d in docs if d.get("id") in ast_filter]
        elif pipeline_filter:
            # Assistants referencing any exported destination / model
            kept_dst = {d["id"] for d in resources.get("destinations", [])}
            kept_mdl = {d["id"] for d in resources.get("models", [])}
            docs = [
                a for a in docs
                if a.get("model_id") in kept_mdl
                or any(did in kept_dst for did in a.get("destination_ids") or [])
            ]
        resources["assistants"] = docs

    active_filter: dict[str, list[str]] = {}
    if pipeline_filter: active_filter["pipeline_ids"] = sorted(pipeline_filter)
    if src_filter:      active_filter["source_ids"] = sorted(src_filter)
    if dst_filter:      active_filter["destination_ids"] = sorted(dst_filter)
    if mdl_filter:      active_filter["model_ids"] = sorted(mdl_filter)
    if ast_filter:      active_filter["assistant_ids"] = sorted(ast_filter)

    bundle: dict[str, Any] = {
        "omnivec_export_version": _EXPORT_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "includes_secrets": bool(include_secrets),
        "includes_checkpoints": bool(include_checkpoints),
        "filter": active_filter or None,
        "resources": resources,
    }

    if include_checkpoints:
        cp_docs = [_strip_internal(d) for d in store.list("checkpoint")]
        # Scope checkpoints to the source_ids actually in the exported bundle
        exported_src_ids = {d["id"] for d in resources.get("sources", [])}
        if exported_src_ids and (src_filter or pipeline_filter):
            cp_docs = [c for c in cp_docs if c.get("source_id") in exported_src_ids]
        bundle["checkpoints"] = cp_docs
    else:
        bundle["checkpoints"] = []

    headers = {}
    if download:
        fname = "omnivec-export-" + datetime.utcnow().strftime("%Y%m%d-%H%M%S") + ".json"
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return JSONResponse(content=bundle, headers=headers)


def _rewrite_ids(bundle_resources: dict, id_map: dict[str, str]) -> None:
    """In-place rewrite of cross-references in a bundle's resources using id_map.

    id_map maps OLD id -> NEW id (for sources, destinations, models, assistants,
    pipelines). Callers populate it only for renamed resources.
    """
    if not id_map:
        return
    for p in bundle_resources.get("pipelines", []):
        for ps in p.get("sources", []) or []:
            if isinstance(ps, dict) and ps.get("source_id") in id_map:
                ps["source_id"] = id_map[ps["source_id"]]
        if p.get("destination_id") in id_map:
            p["destination_id"] = id_map[p["destination_id"]]
        if p.get("docgrok_pipeline") in id_map:
            p["docgrok_pipeline"] = id_map[p["docgrok_pipeline"]]
    for a in bundle_resources.get("assistants", []):
        if a.get("model_id") in id_map:
            a["model_id"] = id_map[a["model_id"]]
        a["destination_ids"] = [id_map.get(d, d) for d in (a.get("destination_ids") or [])]


def _new_suffixed_id(old_id: str) -> str:
    """Generate a new id from an old one by appending -copy-<rand4>."""
    suffix = secrets.token_hex(2)
    if old_id:
        return f"{old_id}-copy-{suffix}"
    return f"copy-{suffix}"


@app.post("/api/admin/import")
async def import_bundle(
    payload: dict,
    on_conflict: str = "skip",
    dry_run: bool = False,
):
    """Import an OmniVec deployment bundle.

    Query params:
      - on_conflict: skip | overwrite | rename (default skip)
      - dry_run: if true, no writes; response describes what would happen
    """
    if on_conflict not in ("skip", "overwrite", "rename"):
        raise HTTPException(status_code=400, detail="on_conflict must be one of: skip, overwrite, rename")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Bundle must be a JSON object")

    version = payload.get("omnivec_export_version")
    if version and str(version).split(".")[0] != _EXPORT_VERSION.split(".")[0]:
        raise HTTPException(status_code=400, detail=f"Unsupported bundle version: {version}")

    resources = dict(payload.get("resources") or {})
    # Deep-copy so we can safely rewrite ids without mutating caller's payload
    import copy
    resources = copy.deepcopy(resources)
    checkpoints = payload.get("checkpoints") or []

    store = get_store()

    # Pre-load existing IDs per type to detect conflicts
    existing: dict[str, set] = {}
    for rtype, doc_type in _EXPORT_RESOURCE_TYPES.items():
        try:
            existing[rtype] = {d["id"] for d in store.list(doc_type) if d.get("id")}
        except Exception:
            existing[rtype] = set()

    summary: dict[str, dict] = {rtype: {"created": 0, "overwritten": 0, "skipped": 0, "renamed": 0, "errors": []}
                                for rtype in _EXPORT_RESOURCE_TYPES}
    summary["checkpoints"] = {"created": 0, "overwritten": 0, "skipped": 0, "renamed": 0, "errors": []}
    warnings: list[str] = []
    id_map: dict[str, str] = {}  # old -> new (for rename mode)
    to_write: list[tuple[str, dict]] = []  # (doc_type, doc)

    # Stable order: sources/destinations/models first (referenced), then pipelines, then assistants
    order = ["sources", "destinations", "models", "assistants", "pipelines"]
    for rtype in order:
        doc_type = _EXPORT_RESOURCE_TYPES[rtype]
        items = resources.get(rtype) or []
        for item in items:
            if not isinstance(item, dict):
                summary[rtype]["errors"].append("item is not an object")
                continue
            item_id = item.get("id")
            if not item_id:
                summary[rtype]["errors"].append("item missing id")
                continue

            # Redacted secret detection
            if isinstance(item.get("config"), dict):
                for k, v in item["config"].items():
                    if v == "***":
                        warnings.append(f"{rtype}/{item_id}: field '{k}' is redacted ('***'); resource may not function until updated")

            conflict = item_id in existing.get(rtype, set())
            if conflict:
                if on_conflict == "skip":
                    summary[rtype]["skipped"] += 1
                    continue
                elif on_conflict == "overwrite":
                    summary[rtype]["overwritten"] += 1
                elif on_conflict == "rename":
                    new_id = _new_suffixed_id(item_id)
                    # Ensure new_id doesn't also collide
                    while new_id in existing.get(rtype, set()):
                        new_id = _new_suffixed_id(item_id)
                    id_map[item_id] = new_id
                    item["id"] = new_id
                    # Also rename to avoid unique-name constraint in most resources
                    if item.get("name"):
                        item["name"] = f"{item['name']}-copy-{new_id[-4:]}"
                    summary[rtype]["renamed"] += 1
                    existing[rtype].add(new_id)
            else:
                summary[rtype]["created"] += 1
                existing[rtype].add(item_id)

            # Pipelines: force paused on import so we never auto-start ingestion
            if rtype == "pipelines":
                item["status"] = "paused"

            to_write.append((doc_type, item))

    # Rewrite cross references after we know the id_map
    if id_map:
        _rewrite_ids(resources, id_map)
        # Because to_write holds references into resources (same dicts), rewrites propagate

    # Checkpoints
    if checkpoints:
        # Pre-load existing checkpoint IDs
        try:
            cp_existing = {d["id"] for d in store.list("checkpoint") if d.get("id")}
        except Exception:
            cp_existing = set()
        cp_to_write = []
        for cp in checkpoints:
            if not isinstance(cp, dict) or not cp.get("id"):
                summary["checkpoints"]["errors"].append("checkpoint missing id")
                continue
            # Rewrite source_id if its source was renamed
            if cp.get("source_id") in id_map:
                cp["source_id"] = id_map[cp["source_id"]]
            cp_id = cp["id"]
            conflict = cp_id in cp_existing
            if conflict and on_conflict == "skip":
                summary["checkpoints"]["skipped"] += 1
                continue
            if conflict and on_conflict == "overwrite":
                summary["checkpoints"]["overwritten"] += 1
            elif not conflict:
                summary["checkpoints"]["created"] += 1
            # rename for checkpoints: keep id (they're regenerated via source_id anyway)
            cp["doc_type"] = "checkpoint"
            cp_to_write.append(cp)
    else:
        cp_to_write = []

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "on_conflict": on_conflict,
            "summary": summary,
            "warnings": warnings,
            "id_map": id_map,
        }

    # Apply writes
    # For external embedding models we must also register with DocGrok so they
    # appear in GET /api/models (which is sourced from the DocGrok registry for
    # non-chat models). Chat models are surfaced via the CosmosDB fallback and
    # don't need DocGrok registration.
    model_docs_to_register: list[dict] = []
    for doc_type, item in to_write:
        try:
            doc = {**item, "doc_type": doc_type}
            if doc_type == "docgrok_model":
                # Route secrets through Key Vault so we don't persist plaintext
                # api keys into the Cosmos doc (mirrors create_model behaviour).
                api_key_value = doc.get("api_key") or ""
                if api_key_value and api_key_value != "***":
                    try:
                        from keyvault_client import set_model_api_key
                        if set_model_api_key(doc.get("id", ""), api_key_value):
                            doc.pop("api_key", None)
                            doc["api_key_source"] = "keyvault"
                    except Exception as kv_err:
                        warnings.append(
                            f"models/{doc.get('id')}: key vault write failed ({kv_err}); "
                            f"api_key will be stored in CosmosDB as a fallback"
                        )
                elif api_key_value == "***":
                    # Redacted bundle — clear the placeholder so we don't store it.
                    doc.pop("api_key", None)
                    warnings.append(
                        f"models/{doc.get('id')}: api_key is redacted; update the "
                        f"model's credentials via PUT /api/models/{doc.get('id')} "
                        f"before use"
                    )
                # Remember models that need DocGrok registration after the write
                if (doc.get("model_category") or "embedding") != "chat" \
                        and str(doc.get("id", "")).startswith("mdl-ext-"):
                    # Keep the plaintext key (if any) around for the registration
                    # call only; the persisted doc above has already been scrubbed.
                    model_docs_to_register.append({**doc, "_plain_api_key": api_key_value})
            store.upsert(doc)
        except Exception as e:
            rtype = next((k for k, v in _EXPORT_RESOURCE_TYPES.items() if v == doc_type), doc_type)
            summary.setdefault(rtype, {}).setdefault("errors", []).append(f"{item.get('id')}: {e}")

    for cp in cp_to_write:
        try:
            store.upsert(cp)
        except Exception as e:
            summary["checkpoints"]["errors"].append(f"{cp.get('id')}: {e}")

    # Register imported external embedding models with DocGrok so they show up
    # in GET /api/models immediately (list_models reads from the DocGrok
    # registry for non-chat models). Best-effort: a registration failure is
    # reported as a warning — DocGrok will also rehydrate from CosmosDB on its
    # next restart, so the model isn't lost either way.
    for mdoc in model_docs_to_register:
        mid = mdoc.get("id", "")
        plain_key = mdoc.pop("_plain_api_key", "") or ""
        # If the plaintext key wasn't in the bundle but we already have one in
        # Key Vault (e.g. re-import over an existing model), fetch it so the
        # DocGrok registration is functional.
        if (not plain_key or plain_key == "***") and mdoc.get("api_key_source") == "keyvault":
            try:
                from keyvault_client import get_model_api_key
                plain_key = get_model_api_key(mid) or ""
            except Exception:
                plain_key = ""
        reg_payload = {
            "id": mid,
            "name": mdoc.get("name", ""),
            "type": mdoc.get("type", "azure-openai"),
            "endpoint": mdoc.get("endpoint", ""),
            "deployment": mdoc.get("deployment", "") or mdoc.get("name", ""),
            "api_key": plain_key,
            "api_version": mdoc.get("api_version", "2024-06-01"),
            "embedding_dim": int(mdoc.get("embedding_dim", 1536) or 1536),
        }
        auth_type = mdoc.get("auth_type") or ("managed-identity" if not plain_key else "key")
        if auth_type == "managed-identity":
            reg_payload["auth_type"] = "managed-identity"
            if mdoc.get("client_id"):
                reg_payload["client_id"] = mdoc["client_id"]
        try:
            resp = await http_client.post(
                f"{DOCGROK_URL}/admin/models/registry", json=reg_payload
            )
            if resp.status_code >= 400:
                warnings.append(
                    f"models/{mid}: DocGrok registration returned "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            warnings.append(
                f"models/{mid}: DocGrok registration failed ({e}); the model "
                f"will appear after the next DocGrok restart"
            )

    return {
        "success": True,
        "dry_run": False,
        "on_conflict": on_conflict,
        "summary": summary,
        "warnings": warnings,
        "id_map": id_map,
    }


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
