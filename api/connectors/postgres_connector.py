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

# Async PostgreSQL client (asyncpg)
_pool = None


async def get_pool(config: Dict[str, Any]):
    """Get or create connection pool."""
    global _pool
    if _pool is None:
        try:
            import asyncpg
        except ImportError:
            raise ImportError("asyncpg required: pip install asyncpg")

        # Build connection string
        dsn = f"postgresql://{config.get('user', '')}:{config.get('password', '')}@{config['host']}:{config.get('port', 5432)}/{config['database']}"

        ssl_mode = config.get("ssl_mode", "require")
        ssl = ssl_mode not in ("disable", "allow")

        _pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            ssl=ssl if ssl else None,
        )
        logger.info("PostgreSQL connection pool created: %s:%s/%s",
                    config['host'], config.get('port', 5432), config['database'])

    return _pool


async def close_pool():
    """Close connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# =============================================================================
# SOURCE OPERATIONS - Read from PostgreSQL
# =============================================================================

async def get_rows_since(
    config: Dict[str, Any],
    since: Optional[datetime] = None,
    limit: int = 1000,
    content_fields: list = None,
) -> Tuple[List[Dict[str, Any]], Optional[datetime]]:
    """
    Get rows modified since a timestamp.

    Returns:
        Tuple of (rows, max_timestamp) where max_timestamp can be used for next poll
    """
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    ts_col = config.get("timestamp_column", "updated_at")
    content_cols = content_fields or ["content"]

    # Build column list
    columns = [id_col, ts_col] + content_cols
    col_str = ", ".join(f'"{c}"' for c in columns)

    # Build query
    if since:
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            WHERE "{ts_col}" > $1
            ORDER BY "{ts_col}" ASC
            LIMIT $2
        '''
        params = [since, limit]
    else:
        query = f'''
            SELECT {col_str}
            FROM "{table}"
            ORDER BY "{ts_col}" ASC
            LIMIT $1
        '''
        params = [limit]

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return [], since

    # Convert to dicts
    result = []
    max_ts = since

    for row in rows:
        doc = dict(row)
        row_ts = doc.get(ts_col)
        if row_ts and (max_ts is None or row_ts > max_ts):
            max_ts = row_ts

        # Combine content columns into single content field
        content_parts = []
        for col in content_cols:
            val = doc.get(col)
            if val:
                content_parts.append(str(val))
        doc["_content"] = "\n\n".join(content_parts)
        doc["_id"] = str(doc[id_col])

        result.append(doc)

    logger.info("Fetched %d rows from %s (since=%s)", len(result), table, since)
    return result, max_ts


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
    """Create pgvector table and index if not exists."""
    pool = await get_pool(config)

    table = config["table"]
    id_col = config.get("id_column", "id")
    vector_col = config.get("vector_column", "embedding")
    content_col = config.get("content_column", "content")
    metadata_cols = config.get("metadata_columns", ["source_id", "source_ref", "created_at"])
    dimensions = config.get("vector_dimensions", 1024)
    index_type = config.get("index_type", "ivfflat")

    async with pool.acquire() as conn:
        # Enable pgvector extension
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

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

        # Create vector index
        index_name = f"{table}_{vector_col}_idx"
        if index_type == "hnsw":
            m = config.get("hnsw_m", 16)
            ef = config.get("hnsw_ef_construction", 64)
            index_sql = f'''
                CREATE INDEX IF NOT EXISTS "{index_name}"
                ON "{table}"
                USING hnsw ("{vector_col}" vector_cosine_ops)
                WITH (m = {m}, ef_construction = {ef})
            '''
        else:  # ivfflat
            lists = config.get("index_lists", 100)
            index_sql = f'''
                CREATE INDEX IF NOT EXISTS "{index_name}"
                ON "{table}"
                USING ivfflat ("{vector_col}" vector_cosine_ops)
                WITH (lists = {lists})
            '''

        try:
            await conn.execute(index_sql)
        except Exception as e:
            logger.warning("Could not create index (may need more data): %s", e)

    logger.info("Ensured pgvector table: %s", table)


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
                   1 - ("{vector_col}" <=> $1::vector) as similarity
            FROM "{table}"
            {where_clause}
            ORDER BY "{vector_col}" <=> $1::vector
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
        row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
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
