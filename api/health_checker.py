"""OmniVec Health Checker

Periodic health checks for sources, destinations, pipelines, and models.
Runs inside the controller process on a configurable interval.
Results are stored in CosmosDB metadata container (doc_type="health").
"""

from __future__ import annotations

import json
import os
import logging
import subprocess
from datetime import datetime

import httpx
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from models import Source, Destination, Pipeline, SourceType, DestinationType
from store import get_store

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))  # seconds
DOCGROK_URL = os.getenv("DOCGROK_URL", "http://docgrok:80")
CHECK_TIMEOUT = 10  # seconds per check


async def _connect_pg(config: dict):
    """Connect to PostgreSQL, handling both Npgsql-style and URI connection strings."""
    import asyncpg
    conn_str = config.get("connection_string", "")
    if conn_str and not conn_str.startswith("postgres"):
        # Npgsql format: Host=x;Port=y;Database=z;Username=u;Password=p;SSL Mode=...
        parts = dict(p.split("=", 1) for p in conn_str.split(";") if "=" in p)
        host = parts.get("Host", parts.get("Server", config.get("host", "")))
        port = int(parts.get("Port", str(config.get("port", 5432))))
        database = parts.get("Database", config.get("database", ""))
        user = parts.get("Username", parts.get("User Id", config.get("user", "")))
        password = parts.get("Password", config.get("password", ""))
        ssl_mode = parts.get("SSL Mode", config.get("ssl_mode", "require")).lower().replace(" ", "")
        ssl = ssl_mode not in ("disable", "allow")
        return await asyncpg.connect(host=host, port=port, database=database,
                                      user=user, password=password, ssl=ssl)
    elif conn_str:
        return await asyncpg.connect(conn_str)
    else:
        host = config.get("host", "")
        port = int(config.get("port", 5432))
        database = config.get("database", "")
        user = config.get("user", "")
        password = config.get("password", "")
        ssl_mode = config.get("ssl_mode", "require")
        ssl = ssl_mode not in ("disable", "allow")
        return await asyncpg.connect(host=host, port=port, database=database,
                                      user=user, password=password, ssl=ssl)


def _strip_doc(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if not k.startswith("_")}
    d.pop("doc_type", None)
    return d


async def check_source(source: Source, last_event_age_seconds: float = None) -> dict:
    """Check source enabled state, connectivity, and permissions.

    If last_event_age_seconds is provided and recent (< 120s), skip read probes
    since the changefeed is actively reading from this source.
    """
    result = {
        "id": source.id,
        "name": source.name,
        "type": source.type.value if hasattr(source.type, "value") else str(source.type),
        "status": "healthy",
        "checks": [],
        "checked_at": datetime.utcnow().isoformat(),
    }

    # Check if source is enabled
    if not source.enabled:
        result["status"] = "warning"
        result["checks"].append({"check": "enabled", "status": "warn", "detail": "Source is disabled — pipelines using it will not process new data"})
        return result
    result["checks"].append({"check": "enabled", "status": "pass", "detail": "Source is enabled"})

    try:
        if source.type == SourceType.AZURE_BLOB:
            config = source.config
            if config.get("connection_string"):
                client = BlobServiceClient.from_connection_string(config["connection_string"])
            else:
                credential = DefaultAzureCredential()
                client = BlobServiceClient(config["account_url"], credential=credential)

            container_client = client.get_container_client(config["container"])
            props = container_client.get_container_properties()  # lgtm[py/unused-local-variable]
            result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Container '{config['container']}' accessible"})

            cf_recent = last_event_age_seconds is not None and last_event_age_seconds < 120
            if not cf_recent:
                blobs = list(container_client.list_blobs(results_per_page=1))  # lgtm[py/unused-local-variable]
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": "Can list blobs"})
            else:
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"Change feed active ({int(last_event_age_seconds)}s ago)"})

        elif source.type == SourceType.COSMOSDB:
            config = source.config
            credential = DefaultAzureCredential()
            client = CosmosClient(config["endpoint"], credential=credential)
            database = client.get_database_client(config["database"])
            container = database.get_container_client(config["container"])

            props = container.read()  # lgtm[py/unused-local-variable]
            result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Container '{config['container']}' accessible"})

            cf_recent = last_event_age_seconds is not None and last_event_age_seconds < 120
            if not cf_recent:
                items = list(container.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))
                doc_count = items[0] if items else 0
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{doc_count} documents"})
            else:
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"Change feed active ({int(last_event_age_seconds)}s ago)"})

        elif source.type == SourceType.POSTGRESQL:
            config = source.config
            conn = await _connect_pg(config)
            try:
                table = config.get("table", "")
                result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Connected to PostgreSQL"})
                row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{row_count} rows in '{table}'"})
            finally:
                await conn.close()

        elif source.type == SourceType.MSSQL:
            config = source.config
            try:
                import pyodbc
            except ImportError:
                result["checks"].append({"check": "connectivity", "status": "skip", "detail": "pyodbc not installed — MSSQL health checks handled by changefeed connector"})
                return result

            conn_str = config.get("connection_string", "")
            if not conn_str:
                server = config.get("server", config.get("host", ""))
                database = config.get("database", "")
                conn_str = f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Encrypt=yes;TrustServerCertificate=no;"

            conn = pyodbc.connect(conn_str, timeout=CHECK_TIMEOUT)
            try:
                table = config.get("table", "")
                schema = config.get("schema_name", config.get("schema", "dbo"))
                result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Connected to MS SQL"})
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
                row_count = cursor.fetchone()[0]
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{row_count} rows in '{schema}.{table}'"})
            finally:
                conn.close()

        else:
            result["checks"].append({"check": "connectivity", "status": "skip", "detail": f"Unsupported source type: {source.type}"})

    except Exception as e:
        result["status"] = "unhealthy"
        result["checks"].append({"check": "connectivity", "status": "fail", "detail": str(e)[:200]})

    return result


async def check_destination(destination: Destination, last_write_age_seconds: float = None, recent_write_failures: int = 0) -> dict:
    """Check destination enabled state, connectivity, write permission, and vector configuration.

    If last_write_age_seconds is recent (< 120s) and no write failures, skip write probes.
    If there are recent write failures, flag them even if pipeline is active.
    """
    result = {
        "id": destination.id,
        "name": destination.name,
        "type": destination.type.value if hasattr(destination.type, "value") else str(destination.type),
        "status": "healthy",
        "checks": [],
        "checked_at": datetime.utcnow().isoformat(),
    }

    # Check if destination is enabled
    if not destination.enabled:
        result["status"] = "warning"
        result["checks"].append({"check": "enabled", "status": "warn", "detail": "Destination is disabled"})
        return result
    result["checks"].append({"check": "enabled", "status": "pass", "detail": "Destination is enabled"})

    try:
        dest_type = destination.type
        config = destination.config

        if dest_type == DestinationType.COSMOSDB_VECTOR:
            credential = DefaultAzureCredential()
            client = CosmosClient(config["endpoint"], credential=credential)
            database = client.get_database_client(config["database"])
            container = database.get_container_client(config["container"])

            props = container.read()
            result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Container '{config['container']}' accessible"})

            # Check vector embedding policy
            vector_embedding_policy = props.get("vectorEmbeddingPolicy", {})
            vector_embeddings = vector_embedding_policy.get("vectorEmbeddings", [])
            indexing_policy = props.get("indexingPolicy", {})
            vector_indexes = indexing_policy.get("vectorIndexes", [])

            if vector_embeddings:
                ve = vector_embeddings[0]
                dims = ve.get("dimensions")
                path = ve.get("path", "")
                result["checks"].append({
                    "check": "vector_policy",
                    "status": "pass",
                    "detail": f"Vector field '{path}' configured, {dims} dimensions",
                    "dimensions": dims,
                    "vector_field": path.lstrip("/"),
                })
            else:
                result["status"] = "warning"
                result["checks"].append({"check": "vector_policy", "status": "warn", "detail": "No vector embedding policy defined"})

            if vector_indexes:
                vi = vector_indexes[0]
                result["checks"].append({
                    "check": "vector_index",
                    "status": "pass",
                    "detail": f"Index type '{vi.get('type')}' on {vi.get('path')}",
                })
            else:
                result["status"] = "warning"
                result["checks"].append({"check": "vector_index", "status": "warn", "detail": "No vector index defined"})

            # Check read permission
            items = list(container.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))
            doc_count = items[0] if items else 0
            result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{doc_count} documents"})

            # Check write permission
            pipeline_writing = last_write_age_seconds is not None and last_write_age_seconds < 120
            if recent_write_failures > 0:
                result["status"] = "unhealthy"
                result["checks"].append({"check": "write_permission", "status": "fail", "detail": f"{recent_write_failures} write failures in recent pipeline jobs"})
            elif pipeline_writing:
                result["checks"].append({"check": "write_permission", "status": "pass", "detail": f"Pipeline writing ({int(last_write_age_seconds)}s ago)"})
            else:
                try:
                    pk_paths = props.get("partitionKey", {}).get("paths", ["/id"])
                    pk_field = pk_paths[0].lstrip("/") if pk_paths else "id"
                    probe_id = "_omnivec_health_probe"
                    probe_doc = {"id": probe_id, pk_field: probe_id, "doc_type": "_probe", "probe": True}
                    container.upsert_item(probe_doc)
                    container.delete_item(probe_id, partition_key=probe_id)
                    result["checks"].append({"check": "write_permission", "status": "pass", "detail": "Write access confirmed (upsert+delete probe)"})
                except Exception as write_err:
                    result["status"] = "unhealthy"
                    result["checks"].append({"check": "write_permission", "status": "fail", "detail": f"Cannot write to destination: {str(write_err)[:150]}"})

        elif dest_type == DestinationType.PGVECTOR:
            conn = await _connect_pg(config)
            try:
                table = config.get("table", "vectors")
                result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Connected to PostgreSQL"})

                # Check table exists and count rows
                row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{row_count} rows in '{table}'"})

                # Check pgvector extension
                ext = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                if ext:
                    result["checks"].append({"check": "vector_extension", "status": "pass", "detail": f"pgvector extension v{ext} installed"})
                else:
                    result["status"] = "warning"
                    result["checks"].append({"check": "vector_extension", "status": "warn", "detail": "pgvector extension not installed"})

                # Check vector column exists and validate its dimension
                vector_col = config.get("vector_column", config.get("vector_col", "embedding"))
                col_exists = await conn.fetchval(
                    "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
                    table, vector_col)
                if col_exists:
                    result["checks"].append({"check": "vector_column", "status": "pass", "detail": f"Vector column '{vector_col}' exists"})

                    # Validate vector dimension matches configured vector_dimensions
                    try:
                        actual_dim = await conn.fetchval(
                            """
                            SELECT a.atttypmod
                            FROM pg_attribute a
                            JOIN pg_class c ON a.attrelid = c.oid
                            JOIN pg_namespace n ON c.relnamespace = n.oid
                            WHERE c.relname = $1
                              AND a.attname = $2
                              AND a.attnum > 0
                              AND NOT a.attisdropped
                              AND n.nspname = ANY (current_schemas(false))
                            """,
                            table, vector_col,
                        )
                        configured_dim = int(config.get("vector_dimensions", 0) or 0)
                        if actual_dim and actual_dim > 0:
                            if configured_dim and actual_dim != configured_dim:
                                result["status"] = "warning"
                                result["checks"].append({
                                    "check": "vector_dimensions",
                                    "status": "warn",
                                    "detail": f"Column is vector({actual_dim}) but config says {configured_dim} — embeddings will fail to insert"
                                })
                            else:
                                result["checks"].append({
                                    "check": "vector_dimensions",
                                    "status": "pass",
                                    "detail": f"vector({actual_dim})"
                                })
                    except Exception as dim_err:
                        result["checks"].append({
                            "check": "vector_dimensions",
                            "status": "warn",
                            "detail": f"Could not introspect dim: {str(dim_err)[:120]}"
                        })
                else:
                    result["status"] = "warning"
                    result["checks"].append({"check": "vector_column", "status": "warn", "detail": f"Vector column '{vector_col}' not found in '{table}'"})
            finally:
                await conn.close()

        elif dest_type == DestinationType.MSSQL:
            try:
                import pyodbc
            except ImportError:
                result["checks"].append({"check": "connectivity", "status": "skip", "detail": "pyodbc not installed — MSSQL health checks handled by changefeed connector"})
                return result

            conn_str = config.get("connection_string", "")
            if not conn_str:
                server = config.get("server", config.get("host", ""))
                database_name = config.get("database", "")
                conn_str = f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database_name};Encrypt=yes;TrustServerCertificate=no;"

            conn = pyodbc.connect(conn_str, timeout=CHECK_TIMEOUT)
            try:
                table = config.get("table", "vectors")
                schema = config.get("schema_name", config.get("schema", "dbo"))
                result["checks"].append({"check": "connectivity", "status": "pass", "detail": f"Connected to MS SQL"})
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
                row_count = cursor.fetchone()[0]
                result["checks"].append({"check": "read_permission", "status": "pass", "detail": f"{row_count} rows in '{schema}.{table}'"})
            finally:
                conn.close()

        else:
            result["checks"].append({"check": "connectivity", "status": "skip", "detail": f"Unsupported destination type: {dest_type}"})

    except Exception as e:
        result["status"] = "unhealthy"
        result["checks"].append({"check": "connectivity", "status": "fail", "detail": str(e)[:200]})

    return result


async def check_model(model_id: str, client: httpx.AsyncClient) -> dict:
    """Check if a DocGrok model is accessible — registry, endpoint reachability, and readiness."""
    result = {
        "id": model_id,
        "name": model_id,
        "status": "healthy",
        "checks": [],
        "checked_at": datetime.utcnow().isoformat(),
    }

    try:
        # Check if DocGrok is reachable
        resp = await client.get(f"{DOCGROK_URL}/health", timeout=CHECK_TIMEOUT)
        if resp.status_code != 200:
            result["status"] = "unhealthy"
            result["checks"].append({"check": "docgrok_health", "status": "fail", "detail": f"DocGrok unhealthy: HTTP {resp.status_code}"})
            return result
        result["checks"].append({"check": "docgrok_health", "status": "pass", "detail": "DocGrok reachable"})

        # Look up model in DocGrok registry
        resp = await client.get(f"{DOCGROK_URL}/admin/models/registry/{model_id}", timeout=CHECK_TIMEOUT)
        if resp.status_code == 200:
            model_info = resp.json()
            result["name"] = model_info.get("name", model_id)

            if model_id.startswith("mdl-native-"):
                # Native model — check K8s deployment status
                ready = model_info.get("ready_replicas", 0)
                desired = model_info.get("replicas", 0)
                status = model_info.get("status", "unknown")
                if ready >= desired and desired > 0:
                    result["checks"].append({"check": "model_ready", "status": "pass", "detail": f"{ready}/{desired} replicas ready, status={status}"})
                else:
                    result["status"] = "unhealthy"
                    result["checks"].append({"check": "model_ready", "status": "fail", "detail": f"{ready}/{desired} replicas ready, status={status}"})
            else:
                # External model — check it's configured
                result["checks"].append({"check": "model_registered", "status": "pass", "detail": f"Model '{result['name']}' registered in DocGrok (type: {model_info.get('type', '?')})"})

                endpoint = model_info.get("endpoint", "")
                if not endpoint:
                    result["status"] = "warning"
                    result["checks"].append({"check": "endpoint_accessible", "status": "warn", "detail": "No endpoint configured for external model"})
                else:
                    # Delegate the actual auth probe to DocGrok — it owns the
                    # decrypted api_key (envelope) and never exposes it to api.py.
                    try:
                        hc = await client.post(
                            f"{DOCGROK_URL}/admin/models/registry/{model_id}/healthcheck",
                            timeout=CHECK_TIMEOUT,
                        )
                        if hc.status_code == 200:
                            hcd = hc.json()
                            ok = hcd.get("ok", False)
                            detail = hcd.get("detail", "")
                            status_code = hcd.get("status", 0)
                            if ok:
                                result["checks"].append({"check": "endpoint_accessible", "status": "pass", "detail": detail or f"HTTP {status_code}"})
                            else:
                                result["status"] = "unhealthy"
                                if status_code in (401, 403):
                                    result["checks"].append({"check": "endpoint_accessible", "status": "fail", "detail": f"Auth failed (HTTP {status_code}): {detail}"})
                                elif status_code == 404:
                                    result["checks"].append({"check": "endpoint_accessible", "status": "fail", "detail": f"Deployment not found at {endpoint} (HTTP 404)"})
                                else:
                                    result["checks"].append({"check": "endpoint_accessible", "status": "fail", "detail": f"HTTP {status_code}: {detail}"})
                        else:
                            result["status"] = "unhealthy"
                            result["checks"].append({"check": "endpoint_accessible", "status": "fail", "detail": f"DocGrok healthcheck HTTP {hc.status_code}: {hc.text[:150]}"})
                    except Exception as ep_err:
                        result["status"] = "unhealthy"
                        result["checks"].append({"check": "endpoint_accessible", "status": "fail", "detail": f"healthcheck failed: {str(ep_err)[:150]}"})

        elif resp.status_code == 404:
            result["status"] = "unhealthy"
            result["checks"].append({"check": "model_registered", "status": "fail", "detail": f"Model '{model_id}' not found in DocGrok registry"})
        else:
            result["status"] = "warning"
            result["checks"].append({"check": "model_registered", "status": "warn", "detail": f"Cannot verify model (HTTP {resp.status_code})"})

    except Exception as e:
        result["status"] = "unhealthy"
        result["checks"].append({"check": "docgrok_health", "status": "fail", "detail": str(e)[:200]})

    return result


async def check_pipeline(
    pipeline: Pipeline,
    sources_map: dict[str, Source],
    destinations_map: dict[str, Destination],
    source_health: dict[str, dict],
    dest_health: dict[str, dict],
    model_health: dict[str, dict],
    client: httpx.AsyncClient,
) -> dict:
    """Check pipeline health: source/dest exist, model accessible, dimensions match.

    A pipeline is unhealthy if any of its sources, its destination, or its model is unhealthy.
    """
    result = {
        "id": pipeline.id,
        "name": pipeline.name,
        "status": "healthy",
        "checks": [],
        "checked_at": datetime.utcnow().isoformat(),
    }

    def _escalate(new_status):
        """Escalate pipeline status: healthy -> warning -> unhealthy."""
        if new_status == "unhealthy":
            result["status"] = "unhealthy"
        elif new_status == "warning" and result["status"] == "healthy":
            result["status"] = "warning"

    # Check sources exist, are enabled, and are healthy
    for ps in pipeline.sources:
        src = sources_map.get(ps.source_id)
        if src:
            # Check enabled state explicitly
            if not src.enabled:
                _escalate("unhealthy")
                result["checks"].append({"check": "source_enabled", "status": "fail", "detail": f"Source '{src.name}' is DISABLED — pipeline cannot process data. Enable it via PUT /api/sources/{src.id}"})
                continue

            sh = source_health.get(ps.source_id, {})
            src_status = sh.get("status", "unknown")
            if src_status in ("unhealthy", "error"):
                _escalate("unhealthy")
                failed_checks = [c["detail"] for c in sh.get("checks", []) if c["status"] == "fail"]
                fail_detail = "; ".join(failed_checks) if failed_checks else src_status
                result["checks"].append({"check": "source_health", "status": "fail", "detail": f"Source '{src.name}' is unhealthy: {fail_detail[:200]}"})
            elif src_status == "warning":
                _escalate("warning")
                warn_checks = [c["detail"] for c in sh.get("checks", []) if c["status"] == "warn"]
                warn_detail = "; ".join(warn_checks) if warn_checks else "has warnings"
                result["checks"].append({"check": "source_health", "status": "warn", "detail": f"Source '{src.name}': {warn_detail[:200]}"})
            else:
                result["checks"].append({"check": "source_health", "status": "pass", "detail": f"Source '{src.name}' ({ps.source_id}) is healthy and enabled"})
        else:
            _escalate("unhealthy")
            result["checks"].append({"check": "source_exists", "status": "fail", "detail": f"Source '{ps.source_id}' not found — it may have been deleted"})

    # Check destination exists, is enabled, and is healthy (connectable + writable)
    dest = destinations_map.get(pipeline.destination_id)
    if dest:
        if not dest.enabled:
            _escalate("unhealthy")
            result["checks"].append({"check": "destination_enabled", "status": "fail", "detail": f"Destination '{dest.name}' is DISABLED — pipeline cannot write results. Enable it via PUT /api/destinations/{dest.id}"})
        else:
            dh = dest_health.get(pipeline.destination_id, {})
            dest_status = dh.get("status", "unknown")
            if dest_status in ("unhealthy", "error"):
                _escalate("unhealthy")
                failed_checks = [c["detail"] for c in dh.get("checks", []) if c["status"] == "fail"]
                fail_detail = "; ".join(failed_checks) if failed_checks else dest_status
                result["checks"].append({"check": "destination_health", "status": "fail", "detail": f"Destination '{dest.name}' is unhealthy: {fail_detail[:200]}"})
            elif dest_status == "warning":
                _escalate("warning")
                warn_checks = [c["detail"] for c in dh.get("checks", []) if c["status"] == "warn"]
                warn_detail = "; ".join(warn_checks) if warn_checks else "has warnings"
                result["checks"].append({"check": "destination_health", "status": "warn", "detail": f"Destination '{dest.name}': {warn_detail[:200]}"})
            else:
                result["checks"].append({"check": "destination_health", "status": "pass", "detail": f"Destination '{dest.name}' ({pipeline.destination_id}) is healthy, writable, and enabled"})
    else:
        _escalate("unhealthy")
        result["checks"].append({"check": "destination_exists", "status": "fail", "detail": f"Destination '{pipeline.destination_id}' not found — it may have been deleted"})

    # Check DocGrok model or transform pipeline reference
    dgp = pipeline.docgrok_pipeline
    try:
        if dgp.startswith("mdl-"):
            # Model ID — check via pre-computed model health
            mh = model_health.get(dgp)
            if mh:
                if mh["status"] == "healthy":
                    result["checks"].append({"check": "model_accessible", "status": "pass", "detail": f"Model '{dgp}' ({mh.get('name', '')}) is healthy"})
                elif mh["status"] in ("unhealthy", "error"):
                    _escalate("unhealthy")
                    failed_checks = [c["detail"] for c in mh.get("checks", []) if c["status"] == "fail"]
                    result["checks"].append({"check": "model_accessible", "status": "fail", "detail": f"Model '{dgp}' is unhealthy: {'; '.join(failed_checks) or mh['status']}"})
                else:
                    _escalate("warning")
                    warn_checks = [c["detail"] for c in mh.get("checks", []) if c["status"] in ("fail", "warn")]
                    result["checks"].append({"check": "model_accessible", "status": "warn", "detail": f"Model '{dgp}': {'; '.join(warn_checks) or mh['status']}"})
            else:
                # Direct check against DocGrok registry
                resp = await client.get(f"{DOCGROK_URL}/admin/models/registry/{dgp}", timeout=CHECK_TIMEOUT)
                if resp.status_code == 200:
                    result["checks"].append({"check": "model_accessible", "status": "pass", "detail": f"Model '{dgp}' registered in DocGrok"})
                else:
                    _escalate("warning")
                    result["checks"].append({"check": "model_accessible", "status": "warn", "detail": f"Model '{dgp}' not found in DocGrok registry"})
        elif dgp in ("mock-embedding", "mock-1536"):
            result["checks"].append({"check": "model_accessible", "status": "pass", "detail": f"Mock pipeline '{dgp}'"})
        else:
            # Transform pipeline name
            resp = await client.get(f"{DOCGROK_URL}/admin/pipelines/{dgp}", timeout=CHECK_TIMEOUT)
            if resp.status_code == 200:
                result["checks"].append({"check": "docgrok_pipeline", "status": "pass", "detail": f"Transform pipeline '{dgp}' exists"})
            else:
                _escalate("unhealthy")
                result["checks"].append({"check": "docgrok_pipeline", "status": "fail", "detail": f"Pipeline or model '{dgp}' not found"})
    except Exception as e:
        result["status"] = "unhealthy"
        result["checks"].append({"check": "docgrok_pipeline", "status": "fail", "detail": f"Cannot reach DocGrok: {str(e)[:100]}"})


    # Check dimension match between destination vector policy and model output
    if dest and pipeline.destination_id in dest_health:
        dh = dest_health[pipeline.destination_id]
        dest_dims = None
        for chk in dh.get("checks", []):
            if chk.get("check") == "vector_policy" and chk.get("dimensions"):
                dest_dims = chk["dimensions"]
                break

        if dest_dims:
            # We can't easily know the model output dimensions without a test embed,
            # but we can note the destination expects N dimensions for awareness
            result["checks"].append({
                "check": "dimension_info",
                "status": "info",
                "detail": f"Destination expects {dest_dims} dimensions",
                "dimensions": dest_dims,
            })

    return result


def check_hpa_saturation() -> list[dict]:
    """Check if any HPA is saturated (at max replicas with CPU above target)."""
    results = []
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        k8s_config.load_incluster_config()
        autoscaling_v2 = k8s_client.AutoscalingV2Api()
        hpa_list = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler("omnivec")

        for hpa in hpa_list.items:
            name = hpa.metadata.name
            max_replicas = hpa.spec.max_replicas
            current_replicas = hpa.status.current_replicas or 0

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

            at_max = current_replicas >= max_replicas
            saturated = at_max and current_cpu is not None and target_cpu is not None and current_cpu > target_cpu

            if saturated:
                results.append({
                    "id": f"hpa-saturation-{name}",
                    "name": f"HPA: {name}",
                    "status": "warning",
                    "checks": [{
                        "check": "hpa_saturation",
                        "status": "warn",
                        "detail": (
                            f"{name} is at max replicas ({current_replicas}/{max_replicas}) "
                            f"with CPU at {current_cpu}% (target: {target_cpu}%). "
                            f"Increase max_replicas via POST /api/operations/deployments/{name}/scale"
                        ),
                    }],
                    "checked_at": datetime.utcnow().isoformat(),
                })
            elif at_max:
                results.append({
                    "id": f"hpa-saturation-{name}",
                    "name": f"HPA: {name}",
                    "status": "healthy",
                    "checks": [{
                        "check": "hpa_saturation",
                        "status": "pass",
                        "detail": f"{name} at max replicas ({current_replicas}/{max_replicas}), CPU: {current_cpu}% (target: {target_cpu}%)",
                    }],
                    "checked_at": datetime.utcnow().isoformat(),
                })
    except Exception as e:
        results.append({
            "id": "hpa-saturation-error",
            "name": "HPA Saturation Check",
            "status": "warning",
            "checks": [{"check": "hpa_saturation", "status": "warn", "detail": f"Cannot check HPAs: {str(e)[:100]}"}],
            "checked_at": datetime.utcnow().isoformat(),
        })
    return results


async def check_services(client: httpx.AsyncClient) -> list[dict]:
    """Check internal service connectivity (changefeed → API, API → DocGrok) and HPA saturation."""
    results = []

    # Check HPA saturation first
    try:
        hpa_results = check_hpa_saturation()
        results.extend(hpa_results)
    except Exception as e:
        logger.warning("HPA saturation check failed: %s", e)

    # Check changefeed → API connectivity by reading its configured URL from K8s
    try:
        raw = subprocess.check_output(
            ["kubectl", "get", "deployment", "omnivec-changefeed", "-n", "omnivec",
             "-o", "jsonpath={.spec.template.spec.containers[0].env}"],
            timeout=5, text=True,
        )
        envs = json.loads(raw) if raw else []
        api_url = next((e["value"] for e in envs if e.get("name") == "ChangeFeed__OmniVecApiBaseUrl"), None)
        if api_url:
            try:
                resp = await client.get(f"{api_url}/health", timeout=5)
                if resp.status_code == 200:
                    results.append({
                        "id": "changefeed-api-connectivity",
                        "name": "ChangeFeed → API",
                        "status": "healthy",
                        "checks": [{"check": "reachable", "status": "pass", "detail": f"{api_url}/health → 200"}],
                        "checked_at": datetime.utcnow().isoformat(),
                    })
                else:
                    results.append({
                        "id": "changefeed-api-connectivity",
                        "name": "ChangeFeed → API",
                        "status": "unhealthy",
                        "checks": [{"check": "reachable", "status": "fail", "detail": f"{api_url}/health → HTTP {resp.status_code}"}],
                        "checked_at": datetime.utcnow().isoformat(),
                    })
            except Exception as e:
                results.append({
                    "id": "changefeed-api-connectivity",
                    "name": "ChangeFeed → API",
                    "status": "unhealthy",
                    "checks": [{"check": "reachable", "status": "fail", "detail": f"Cannot reach {api_url}: {str(e)[:100]}"}],
                    "checked_at": datetime.utcnow().isoformat(),
                })
    except Exception as e:
        results.append({
            "id": "changefeed-api-connectivity",
            "name": "ChangeFeed → API",
            "status": "warning",
            "checks": [{"check": "config_read", "status": "warn", "detail": f"Cannot read changefeed config: {str(e)[:100]}"}],
            "checked_at": datetime.utcnow().isoformat(),
        })

    # Check API → DocGrok connectivity
    try:
        resp = await client.get(f"{DOCGROK_URL}/health", timeout=5)
        if resp.status_code == 200:
            results.append({
                "id": "api-docgrok-connectivity",
                "name": "API → DocGrok",
                "status": "healthy",
                "checks": [{"check": "reachable", "status": "pass", "detail": f"{DOCGROK_URL}/health → 200"}],
                "checked_at": datetime.utcnow().isoformat(),
            })
        else:
            results.append({
                "id": "api-docgrok-connectivity",
                "name": "API → DocGrok",
                "status": "unhealthy",
                "checks": [{"check": "reachable", "status": "fail", "detail": f"HTTP {resp.status_code}"}],
                "checked_at": datetime.utcnow().isoformat(),
            })
    except Exception as e:
        results.append({
            "id": "api-docgrok-connectivity",
            "name": "API → DocGrok",
            "status": "unhealthy",
            "checks": [{"check": "reachable", "status": "fail", "detail": str(e)[:100]}],
            "checked_at": datetime.utcnow().isoformat(),
        })

    return results


async def run_health_checks(section: str | None = None) -> dict:
    """Run health checks. If section is specified, only run checks for that section
    and merge results into existing stored data.
    section: 'sources', 'destinations', 'pipelines', 'models', 'services', or None for all.
    """
    store = get_store()

    # If section-specific, load existing data to merge into
    existing = None
    if section:
        existing = store.get("health_status", "health")
        if not existing:
            existing = {"id": "health_status", "doc_type": "health", "overall": "unknown",
                        "checked_at": None, "sources": [], "destinations": [],
                        "pipelines": [], "models": [], "services": [], "summary": {}}

    # Load all resources
    source_docs = store.list("source")
    dest_docs = store.list("destination")
    pipeline_docs = store.list("pipeline")

    sources = [Source(**_strip_doc(d)) for d in source_docs]
    destinations = [Destination(**_strip_doc(d)) for d in dest_docs]
    pipelines = [Pipeline(**_strip_doc(d)) for d in pipeline_docs]

    sources_map = {s.id: s for s in sources}
    destinations_map = {d.id: d for d in destinations}

    # Compute pipeline activity: last event age per source, last write age + failures per destination
    now = datetime.utcnow()
    source_last_event = {}  # source_id -> seconds since last event
    dest_last_write = {}    # dest_id -> seconds since last write
    dest_write_fails = {}   # dest_id -> count of recent write failures

    active_pipelines = [p for p in pipelines if (p.status.value if hasattr(p.status, "value") else p.status) == "active"]  # lgtm[py/unused-local-variable]
    for pip in pipelines:
        pip_status = pip.status.value if hasattr(pip.status, "value") else pip.status
        if pip_status != "active":
            continue
        # Check recent jobs for this pipeline
        try:
            job_docs = store.query(
                "SELECT TOP 20 * FROM c WHERE c.doc_type = 'job' AND c.pipeline_id = @pid ORDER BY c.created_at DESC",
                partition_key="job",
                parameters=[{"name": "@pid", "value": pip.id}],
            )
            for jd in job_docs:
                # Source activity
                for ps in pip.sources:
                    sid = ps.source_id if hasattr(ps, "source_id") else ps.get("source_id")
                    completed = jd.get("completed_at") or jd.get("started_at")
                    if completed and sid:
                        try:
                            t = datetime.fromisoformat(completed.replace("Z", "+00:00").replace("+00:00", ""))
                            age = (now - t).total_seconds()
                            if sid not in source_last_event or age < source_last_event[sid]:
                                source_last_event[sid] = age
                        except (ValueError, TypeError):  # lgtm[py/empty-except]
                            pass
                # Destination activity
                did = pip.destination_id
                completed = jd.get("completed_at")
                if completed and did:
                    try:
                        t = datetime.fromisoformat(completed.replace("Z", "+00:00").replace("+00:00", ""))
                        age = (now - t).total_seconds()
                        if did not in dest_last_write or age < dest_last_write[did]:
                            dest_last_write[did] = age
                    except (ValueError, TypeError):  # lgtm[py/empty-except]
                        pass
                # Write failures
                if jd.get("status") == "failed" and did:
                    err = str(jd.get("error", ""))
                    if "write" in err.lower() or "upsert" in err.lower() or "403" in err or "NotFound" in err:
                        dest_write_fails[did] = dest_write_fails.get(did, 0) + 1
        except Exception:  # lgtm[py/empty-except]
            pass

    # Run source checks
    source_results = []
    source_health_map = {}
    if not section or section == "sources":
        for source in sources:
            try:
                sr = await check_source(source, last_event_age_seconds=source_last_event.get(source.id))
                source_results.append(sr)
                source_health_map[source.id] = sr
            except Exception as e:
                sr = {
                    "id": source.id, "name": source.name, "status": "error",
                    "checks": [{"check": "unexpected", "status": "fail", "detail": str(e)[:200]}],
                    "checked_at": datetime.utcnow().isoformat(),
                }
                source_results.append(sr)
                source_health_map[source.id] = sr
    elif existing:
        # Use cached source results for pipeline checks
        source_results = existing.get("sources", [])
        source_health_map = {r["id"]: r for r in source_results}

    # Run destination checks
    dest_results = []
    dest_health_map = {}
    if not section or section == "destinations":
        for dest in destinations:
            try:
                dr = await check_destination(
                    dest,
                    last_write_age_seconds=dest_last_write.get(dest.id),
                    recent_write_failures=dest_write_fails.get(dest.id, 0),
                )
                dest_results.append(dr)
                dest_health_map[dest.id] = dr
            except Exception as e:
                dr = {
                    "id": dest.id, "name": dest.name, "status": "error",
                    "checks": [{"check": "unexpected", "status": "fail", "detail": str(e)[:200]}],
                    "checked_at": datetime.utcnow().isoformat(),
                }
                dest_results.append(dr)
                dest_health_map[dest.id] = dr
    elif existing:
        dest_results = existing.get("destinations", [])
        dest_health_map = {r["id"]: r for r in dest_results}

    # Collect unique models from pipelines' DocGrok references
    async with httpx.AsyncClient(timeout=httpx.Timeout(CHECK_TIMEOUT, connect=5.0)) as client:
        model_keys_seen = set()
        model_results = []
        model_health_map = {}

        if not section or section in ("models", "pipelines"):
            if not section or section == "models":
                # Check ALL registered models from DocGrok (not just pipeline-referenced)
                try:
                    reg_resp = await client.get(f"{DOCGROK_URL}/admin/models/registry", timeout=CHECK_TIMEOUT)
                    if reg_resp.status_code == 200:
                        all_models = reg_resp.json()
                        if isinstance(all_models, dict):
                            all_models = all_models.get("models", [])
                        for m in all_models:
                            mid = m.get("id", "")
                            if mid and mid not in model_keys_seen:
                                model_keys_seen.add(mid)
                                try:
                                    mr = await check_model(mid, client)
                                    model_results.append(mr)
                                    model_health_map[mid] = mr
                                except Exception:  # lgtm[py/empty-except]
                                    pass
                except Exception:  # lgtm[py/empty-except]
                    pass
                # Also check any pipeline-referenced models not yet in registry
                for pipeline in pipelines:
                    dgp = pipeline.docgrok_pipeline
                    if dgp.startswith("mdl-") and dgp not in model_keys_seen:
                        model_keys_seen.add(dgp)
                        try:
                            mr = await check_model(dgp, client)
                            model_results.append(mr)
                            model_health_map[dgp] = mr
                        except Exception:  # lgtm[py/empty-except]
                            pass
            elif existing:
                model_results = existing.get("models", [])
                for mr in model_results:
                    key = mr.get("id", mr.get("name", ""))
                    model_health_map[key] = mr
        elif existing:
            model_results = existing.get("models", [])
            for mr in model_results:
                key = mr.get("id", mr.get("name", ""))
                model_health_map[key] = mr

        # Run pipeline checks
        pipeline_results = []
        if not section or section == "pipelines":
            for pipeline in pipelines:
                try:
                    pr = await check_pipeline(
                        pipeline, sources_map, destinations_map,
                        source_health_map, dest_health_map, model_health_map, client,
                    )
                    pipeline_results.append(pr)
                except Exception as e:
                    pipeline_results.append({
                        "id": pipeline.id, "name": pipeline.name, "status": "error",
                        "checks": [{"check": "unexpected", "status": "fail", "detail": str(e)[:200]}],
                        "checked_at": datetime.utcnow().isoformat(),
                    })
        elif existing:
            pipeline_results = existing.get("pipelines", [])

        # Run services connectivity checks
        service_results = []
        if not section or section == "services":
            try:
                service_results = await check_services(client)
            except Exception as e:
                service_results = [{
                    "id": "services-check-error", "name": "Services",
                    "status": "error",
                    "checks": [{"check": "unexpected", "status": "fail", "detail": str(e)[:200]}],
                    "checked_at": datetime.utcnow().isoformat(),
                }]
        elif existing:
            service_results = existing.get("services", [])

    # Build summary
    def _section_summary(results):
        return {
            "total": len(results),
            "healthy": sum(1 for r in results if r.get("status") == "healthy"),
            "unhealthy": sum(1 for r in results if r.get("status") in ("unhealthy", "error")),
        }

    all_statuses = (
        [r["status"] for r in source_results]
        + [r["status"] for r in dest_results]
        + [r["status"] for r in pipeline_results]
        + [r["status"] for r in model_results]
        + [r["status"] for r in service_results]
    )
    if any(s == "unhealthy" or s == "error" for s in all_statuses):
        overall = "unhealthy"
    elif any(s == "warning" for s in all_statuses):
        overall = "warning"
    else:
        overall = "healthy"

    health_doc = {
        "id": "health_status",
        "doc_type": "health",
        "overall": overall,
        "checked_at": datetime.utcnow().isoformat(),
        "sources": source_results,
        "destinations": dest_results,
        "pipelines": pipeline_results,
        "models": model_results,
        "services": service_results,
        "summary": {
            "sources": _section_summary(source_results),
            "destinations": _section_summary(dest_results),
            "pipelines": _section_summary(pipeline_results),
            "models": _section_summary(model_results),
            "services": _section_summary(service_results),
        },
    }

    # Store in CosmosDB
    store.upsert(health_doc)
    logger.info(
        "Health check complete: section=%s overall=%s sources=%d/%d dest=%d/%d pip=%d/%d models=%d/%d",
        section or "all", overall,  # lgtm[py/log-injection]
        health_doc["summary"]["sources"]["healthy"], health_doc["summary"]["sources"]["total"],
        health_doc["summary"]["destinations"]["healthy"], health_doc["summary"]["destinations"]["total"],
        health_doc["summary"]["pipelines"]["healthy"], health_doc["summary"]["pipelines"]["total"],
        health_doc["summary"]["models"]["healthy"], health_doc["summary"]["models"]["total"],
    )

    return health_doc
