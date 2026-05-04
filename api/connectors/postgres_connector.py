#!/usr/bin/env python3
"""PostgreSQL Connector - Read from PostgreSQL, write to pgvector.

Supports:
- Reading rows as documents from PostgreSQL tables
- Polling-based change detection using timestamp column
- Writing embeddings to pgvector tables
- Connection pooling for efficiency
"""

import re
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, AsyncGenerator

logger = logging.getLogger(__name__)

# Async PostgreSQL client (asyncpg). Pool is keyed by (host, port, db, user) so
# multiple sources/destinations don't accidentally share a connection pool that
# was created with a different config.
_pools: Dict[Tuple[str, int, str, str], Any] = {}


def _pool_key(config: Dict[str, Any]) -> Tuple[str, int, str, str]:
    return (
        str(config.get("host", "")),
        int(config.get("port", 5432) or 5432),
        str(config.get("database", "")),
        str(config.get("user", "")),
    )


async def get_pool(config: Dict[str, Any]):
    """Get or create connection pool for the given config."""
    key = _pool_key(config)
    pool = _pools.get(key)
    if pool is not None:
        return pool

    try:
        import asyncpg
    except ImportError:
        raise ImportError("asyncpg required: pip install asyncpg")

    # Build connection string
    dsn = f"postgresql://{config.get('user', '')}:{config.get('password', '')}@{config['host']}:{config.get('port', 5432)}/{config['database']}"

    ssl_mode = config.get("ssl_mode", "require")
    ssl = ssl_mode not in ("disable", "allow")

    pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        ssl=ssl if ssl else None,
    )
    _pools[key] = pool
    logger.info("PostgreSQL connection pool created: %s:%s/%s",
                config['host'], config.get('port', 5432), config['database'])
    return pool


async def close_pool():
    """Close all connection pools."""
    for pool in list(_pools.values()):
        try:
            await pool.close()
        except Exception:  # lgtm[py/empty-except]
            pass
    _pools.clear()


# =============================================================================
# SOURCE OPERATIONS - Read from PostgreSQL
# =============================================================================

async def get_rows_since(
    config: Dict[str, Any],
    since: Optional[datetime] = None,
    limit: int = 1000,
    content_fields: list = None,
    last_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[datetime], Optional[str]]:
    """
    Get rows modified since a timestamp.

    Uses (timestamp, id) lexicographic ordering with an id tiebreaker so that
    rows sharing the exact same timestamp as the last checkpoint are not
    skipped on subsequent polls. Returns a tuple of
    (rows, max_timestamp, max_id) suitable for use as the next checkpoint.
    """
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    ts_col = config.get("timestamp_column", "updated_at")
    content_cols = content_fields or ["content"]

    # Build column list
    columns = [id_col, ts_col] + content_cols
    col_str = ", ".join(f'"{c}"' for c in columns)

    # Build query — always order by (timestamp, id) for deterministic paging.
    if since is not None and last_id is not None:
        # (ts, id) strictly greater than (since, last_id)
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            WHERE ("{ts_col}", "{id_col}"::text) > ($1, $2)
            ORDER BY "{ts_col}" ASC, "{id_col}" ASC
            LIMIT $3
        '''
        params = [since, str(last_id), limit]
    elif since is not None:
        # First poll after a checkpoint that didn't carry an id. Use >= so we
        # don't skip rows that share the checkpoint timestamp; the
        # downstream pipeline already deduplicates by content_hash.
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            WHERE "{ts_col}" >= $1
            ORDER BY "{ts_col}" ASC, "{id_col}" ASC
            LIMIT $2
        '''
        params = [since, limit]
    else:
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            ORDER BY "{ts_col}" ASC, "{id_col}" ASC
            LIMIT $1
        '''
        params = [limit]

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return [], since, last_id

    # Convert to dicts
    result = []
    max_ts = since
    max_id = last_id

    for row in rows:
        doc = dict(row)
        row_ts = doc.get(ts_col)
        row_id = doc.get(id_col)
        if row_ts is not None and (max_ts is None or row_ts >= max_ts):
            if max_ts is None or row_ts > max_ts or row_id is None:
                max_ts = row_ts
                max_id = str(row_id) if row_id is not None else max_id
            else:
                # Same timestamp — keep the larger id as tiebreaker
                rid_str = str(row_id)
                if max_id is None or rid_str > max_id:
                    max_id = rid_str

        # Combine content columns into single content field
        content_parts = []
        for col in content_cols:
            val = doc.get(col)
            if val:
                content_parts.append(str(val))
        doc["_content"] = "\n\n".join(content_parts)
        doc["_id"] = str(doc[id_col])

        result.append(doc)

    logger.info("Fetched %d rows from %s (since=%s, last_id=%s)",
                len(result), table, since, last_id)
    return result, max_ts, max_id


async def get_row_by_id(
    config: Dict[str, Any],
    row_id: str,
    content_fields: list = None,
) -> Optional[Dict[str, Any]]:
    """Get a single row by ID."""
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    content_cols = content_fields or ["content"]

    columns = [id_col] + content_cols
    col_str = ", ".join(f'"{c}"' for c in columns)

    query = f'SELECT {col_str} FROM "{table}" WHERE "{id_col}" = $1'

    # Try to convert row_id to int if it looks like an integer
    try:
        typed_id = int(row_id) if row_id.isdigit() else row_id
    except (ValueError, AttributeError):
        typed_id = row_id

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, typed_id)

    if not row:
        return None

    doc = dict(row)
    content_parts = [str(doc.get(col, "")) for col in content_cols if doc.get(col)]
    doc["_content"] = "\n\n".join(content_parts)
    doc["_id"] = str(doc[id_col])

    return doc


async def count_rows(config: Dict[str, Any]) -> int:
    """Count total rows in table."""
    pool = await get_pool(config)
    table = config["table"]

    async with pool.acquire() as conn:
        result = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')

    return result or 0


async def stream_all_rows(
    config: Dict[str, Any],
    batch_size: int = 100,
    content_fields: list = None,
) -> AsyncGenerator[List[Dict[str, Any]], None]:
    """Stream all rows in batches for initial backfill."""
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    content_cols = content_fields or ["content"]

    columns = [id_col] + content_cols
    col_str = ", ".join(f'"{c}"' for c in columns)

    offset = 0
    while True:
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            ORDER BY "{id_col}"
            LIMIT $1 OFFSET $2
        '''

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, batch_size, offset)

        if not rows:
            break

        batch = []
        for row in rows:
            doc = dict(row)
            content_parts = [str(doc.get(col, "")) for col in content_cols if doc.get(col)]
            doc["_content"] = "\n\n".join(content_parts)
            doc["_id"] = str(doc[id_col])
            batch.append(doc)

        yield batch
        offset += len(rows)

        if len(rows) < batch_size:
            break


# =============================================================================
# DESTINATION OPERATIONS - Write to pgvector
# =============================================================================

async def ensure_pgvector_table(config: Dict[str, Any]):
    """Create pgvector table and index if not exists.

    If the table already exists with a different vector dimension than the
    configured one, logs a clear error so callers can surface it instead of
    failing later with an opaque pgvector cast error.
    """
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    vector_col = config.get("vector_column", "embedding")
    content_col = config.get("content_column", "content")
    metadata_cols = config.get("metadata_columns", ["source_id", "source_ref", "created_at"])
    dimensions = int(config.get("vector_dimensions", 1536))
    index_type = config.get("index_type", "hnsw")
    metric = str(config.get("metric", "cosine")).lower()

    # Map metric → pgvector index opclass
    if metric in ("l2", "euclidean"):
        opclass = "vector_l2_ops"
    elif metric in ("dot", "inner_product", "ip"):
        opclass = "vector_ip_ops"
    else:
        opclass = "vector_cosine_ops"

    async with pool.acquire() as conn:
        # Enable pgvector extension
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

        # If table+column already exist, validate the dimension matches.
        try:
            existing_dim = await conn.fetchval(
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
            if existing_dim is not None and existing_dim > 0 and existing_dim != dimensions:
                logger.error(
                    "pgvector dim mismatch on %s.%s: existing=vector(%d), config=vector(%d). "
                    "Embeddings will fail to insert. Drop the column or change vector_dimensions.",
                    table, vector_col, existing_dim, dimensions,
                )
        except Exception as e:
            logger.debug("Could not introspect existing vector column dim: %s", e)

        # Build metadata columns SQL
        meta_sql = ", ".join(f'"{col}" TEXT' for col in metadata_cols if col != "created_at")
        if "created_at" in metadata_cols:
            meta_sql += ', "created_at" TIMESTAMPTZ DEFAULT NOW()'

        # Create table
        create_sql = f'''
            CREATE TABLE IF NOT EXISTS "{table}" (
                "{id_col}" TEXT PRIMARY KEY,
                "{content_col}" TEXT,
                "{vector_col}" vector({dimensions}),
                {meta_sql}
            )
        '''
        await conn.execute(create_sql)

        # Create vector index — opclass must match query metric.
        index_name = f"{table}_{vector_col}_idx"
        if index_type == "hnsw":
            m = config.get("hnsw_m", 16)
            ef = config.get("hnsw_ef_construction", 64)
            index_sql = f'''
                CREATE INDEX IF NOT EXISTS "{index_name}"
                ON "{table}"
                USING hnsw ("{vector_col}" {opclass})
                WITH (m = {m}, ef_construction = {ef})
            '''
        else:  # ivfflat
            lists = config.get("index_lists", 100)
            index_sql = f'''
                CREATE INDEX IF NOT EXISTS "{index_name}"
                ON "{table}"
                USING ivfflat ("{vector_col}" {opclass})
                WITH (lists = {lists})
            '''

        try:
            await conn.execute(index_sql)
        except Exception as e:
            logger.warning("Could not create index (may need more data): %s", e)

    logger.info("Ensured pgvector table: %s (dim=%d, metric=%s, index=%s)",
                table, dimensions, metric, index_type)


async def upsert_vectors(
    config: Dict[str, Any],
    documents: List[Dict[str, Any]],
) -> int:
    """
    Upsert documents with embeddings to pgvector table.

    Each document should have:
    - id: Document ID
    - embedding: List[float] vector
    - content: Original text (optional)
    - metadata: Dict of metadata fields
    """
    if not documents:
        return 0

    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    vector_col = config.get("vector_column", "embedding")
    content_col = config.get("content_column", "content")
    metadata_cols = config.get("metadata_columns", ["source_id", "source_ref", "created_at"])

    # Build upsert SQL
    all_cols = [id_col, vector_col, content_col] + [c for c in metadata_cols if c != "created_at"]
    col_str = ", ".join(f'"{c}"' for c in all_cols)
    placeholders = ", ".join(f"${i+1}" for i in range(len(all_cols)))

    update_cols = [c for c in all_cols if c != id_col]
    update_str = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)

    upsert_sql = f'''
        INSERT INTO "{table}" ({col_str})
        VALUES ({placeholders})
        ON CONFLICT ("{id_col}") DO UPDATE SET {update_str}
    '''

    count = 0
    async with pool.acquire() as conn:
        for doc in documents:
            try:
                # Build values
                embedding = doc.get("embedding", [])
                if isinstance(embedding, list):
                    embedding = str(embedding)  # Convert to PostgreSQL array format

                values = [
                    doc.get("id", ""),
                    embedding,
                    doc.get("content", ""),
                ]

                # Add metadata
                metadata = doc.get("metadata", {})
                for col in metadata_cols:
                    if col != "created_at":
                        values.append(str(metadata.get(col, "")))

                await conn.execute(upsert_sql, *values)
                count += 1

            except Exception as e:
                logger.error("Failed to upsert document %s: %s", doc.get("id"), e)

    logger.info("Upserted %d vectors to %s", count, table)
    return count


async def search_vectors(
    config: Dict[str, Any],
    query_vector: List[float],
    top_k: int = 10,
    filter_metadata: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Search for similar vectors.

    Returns list of documents with similarity scores.
    Note: Creates a fresh connection to avoid event loop conflicts when called from API.
    """
    try:
        import asyncpg
    except ImportError:
        raise ImportError("asyncpg required: pip install asyncpg")

    # Build connection string
    dsn = f"postgresql://{config.get('user', '')}:{config.get('password', '')}@{config['host']}:{config.get('port', 5432)}/{config['database']}"
    ssl_mode = config.get("ssl_mode", "require")
    ssl = ssl_mode not in ("disable", "allow")

    # Create fresh connection for search (avoids event loop conflicts)
    conn = await asyncpg.connect(dsn, ssl=ssl if ssl else None)

    try:
        table = config["table"]
        id_col = config.get("id_column", "id")
        vector_col = config.get("vector_column", "embedding")
        content_col = config.get("content_column", "content")
        metadata_cols = config.get("metadata_columns", [])
        metric = str(config.get("metric", "cosine")).lower()

        # Map metric → pgvector operator and similarity expression.
        # cosine: <=> returns cosine distance in [0, 2]; similarity = 1 - dist
        # l2:     <-> returns euclidean distance >= 0;   similarity = 1/(1+dist)
        # dot:    <#> returns negative inner product;    similarity = -(<#>)
        if metric in ("l2", "euclidean"):
            op = "<->"
            sim_expr = f'1.0 / (1.0 + ("{vector_col}" <-> $1::vector))'
        elif metric in ("dot", "inner_product", "ip"):
            op = "<#>"
            sim_expr = f'-("{vector_col}" <#> $1::vector)'
        else:  # cosine (default)
            op = "<=>"
            sim_expr = f'1 - ("{vector_col}" <=> $1::vector)'

        # Build column list
        select_cols = [id_col, content_col] + metadata_cols
        col_str = ", ".join(f'"{c}"' for c in select_cols)

        # Build filter clause
        where_clause = ""
        params = [str(query_vector), top_k]
        if filter_metadata:
            conditions = []
            for i, (key, value) in enumerate(filter_metadata.items()):
                conditions.append(f'"{key}" = ${i+3}')
                params.append(value)
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f'''
            SELECT {col_str},
                   {sim_expr} as similarity
            FROM "{table}"
            {where_clause}
            ORDER BY "{vector_col}" {op} $1::vector
            LIMIT $2
        '''

        rows = await conn.fetch(query, *params)

        results = []
        for row in rows:
            doc = dict(row)
            results.append({
                "id": doc[id_col],
                "content": doc.get(content_col, ""),
                "similarity": float(doc.get("similarity", 0)),
                "metadata": {col: doc.get(col) for col in metadata_cols},
            })

        return results
    finally:
        await conn.close()


async def delete_vectors(
    config: Dict[str, Any],
    ids: List[str],
) -> int:
    """Delete vectors by ID."""
    if not ids:
        return 0

    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")

    placeholders = ", ".join(f"${i+1}" for i in range(len(ids)))
    query = f'DELETE FROM "{table}" WHERE "{id_col}" IN ({placeholders})'

    async with pool.acquire() as conn:
        result = await conn.execute(query, *ids)

    # Parse "DELETE N" result
    count = int(result.split()[-1]) if result else 0
    logger.info("Deleted %d vectors from %s", count, table)
    return count


# =============================================================================
# VECTOR COLUMN DISCOVERY
# =============================================================================

async def _discover_vector_columns(conn, table: str) -> List[Dict[str, Any]]:
    """Discover vector columns in a pgvector table using an existing connection.

    Queries information_schema for columns with the ``vector`` user-defined type,
    then resolves each column's dimension from ``pg_attribute``.  Returns a list
    of descriptors in the same format as CosmosDB ``vector_indexes`` so the UI
    dropdown and pipeline validation work identically.
    """
    rows = await conn.fetch(
        """SELECT column_name, data_type, udt_name
           FROM information_schema.columns
           WHERE table_name = $1
           ORDER BY ordinal_position""",
        table,
    )

    vector_indexes: List[Dict[str, Any]] = []
    for row in rows:
        col_name = row["column_name"]
        data_type = (row["data_type"] or "").lower()
        udt_name = (row["udt_name"] or "").lower()

        if "vector" not in data_type and udt_name != "vector":
            continue

        # Resolve dimensions from vector(N) type modifier
        dimensions = None
        type_str = await conn.fetchval(
            """SELECT format_type(atttypid, atttypmod)
               FROM pg_attribute
               WHERE attrelid = $1::regclass AND attname = $2""",
            table,
            col_name,
        )
        if type_str:
            m = re.search(r"vector\((\d+)\)", str(type_str))
            if m:
                dimensions = int(m.group(1))

        # Check for a vector index on this column
        idx_row = await conn.fetchrow(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = $1 AND indexdef LIKE '%' || $2 || '%'",
            table,
            col_name,
        )
        index_type = None
        index_name = None
        if idx_row:
            index_name = idx_row["indexname"]
            indexdef = idx_row["indexdef"]
            if "ivfflat" in indexdef:
                index_type = "ivfflat"
            elif "hnsw" in indexdef:
                index_type = "hnsw"
            else:
                index_type = "btree"

        vector_indexes.append({
            "path": col_name,
            "dimensions": dimensions,
            "dataType": "vector",
            "indexType": index_type,
            "indexName": index_name,
        })

    return vector_indexes


async def probe_vector_columns(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Discover vector columns using a fresh connection (no pool required).

    Returns ``vector_indexes`` in CosmosDB-compatible format so the UI and
    pipeline validation treat pgvector destinations the same as CosmosDB.
    """
    try:
        import asyncpg
    except ImportError:
        logger.warning("asyncpg not installed — cannot probe pgvector columns")
        return []

    dsn = (
        f"postgresql://{config.get('user', '')}:{config.get('password', '')}"
        f"@{config['host']}:{config.get('port', 5432)}/{config['database']}"
    )
    ssl_mode = config.get("ssl_mode", "require")
    ssl = ssl_mode not in ("disable", "allow")

    conn = await asyncpg.connect(dsn, ssl=ssl if ssl else None)
    try:
        return await _discover_vector_columns(conn, config["table"])
    finally:
        await conn.close()


async def test_destination_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    """Test pgvector destination connectivity and discover vector columns.

    Called from ``create_destination`` and ``test_destination`` in the API layer,
    mirroring ``cosmosdb_vector_connector.test_vector_connection``.
    """
    try:
        import asyncpg
    except ImportError:
        raise ImportError("asyncpg required: pip install asyncpg")

    dsn = (
        f"postgresql://{config.get('user', '')}:{config.get('password', '')}"
        f"@{config['host']}:{config.get('port', 5432)}/{config['database']}"
    )
    ssl_mode = config.get("ssl_mode", "require")
    ssl = ssl_mode not in ("disable", "allow")

    conn = await asyncpg.connect(dsn, ssl=ssl if ssl else None)
    try:
        table = config["table"]
        # Validate identifier to prevent SQL injection through identifier interpolation.
        import re as _re
        if not isinstance(table, str) or not _re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", table):
            raise ValueError(f"invalid table identifier: {table!r}")
        row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')  # lgtm[py/sql-injection]
        ext = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )

        vector_indexes = await _discover_vector_columns(conn, table)

        # First discovered column becomes the default; fall back to config
        vector_field = config.get("vector_column", "embedding")
        if vector_indexes:
            vector_field = vector_indexes[0]["path"]

        return {
            "status": "connected",
            "table": table,
            "row_count": row_count,
            "pgvector_version": ext,
            "vector_field": vector_field,
            "vector_indexes": vector_indexes,
            "has_vector_policy": len(vector_indexes) > 0,
        }
    finally:
        await conn.close()
