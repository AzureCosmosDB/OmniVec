"""CosmosDB Vector Destination Connector"""

import os
import asyncio
import logging
from typing import Dict, Any, List
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity import ManagedIdentityCredential, DefaultAzureCredential

logger = logging.getLogger(__name__)

# Cache: endpoint → CosmosClient (reuse connections + tokens)
_client_cache: Dict[str, CosmosClient] = {}
_credential = None

# Retry configuration - never fail on 429
MAX_RETRIES = 20  # Keep retrying until we succeed
BASE_DELAY_MS = 100
MAX_DELAY_MS = 30000  # Cap at 30 seconds

# Adaptive throttle: starts fast, slows down on 429s
_throttle_delay_ms = 0  # Current delay between operations
_throttle_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None


async def _adaptive_throttle():
    """Apply current throttle delay before operation."""
    global _throttle_delay_ms
    if _throttle_delay_ms > 0:
        await asyncio.sleep(_throttle_delay_ms / 1000.0)


async def _increase_throttle():
    """Increase throttle on 429 (double delay, max 5s)."""
    global _throttle_delay_ms
    _throttle_delay_ms = min(max(_throttle_delay_ms * 2, 100), 5000)
    logger.warning(f"Throttle increased to {_throttle_delay_ms}ms")


async def _decrease_throttle():
    """Decrease throttle on success (halve delay)."""
    global _throttle_delay_ms
    if _throttle_delay_ms > 0:
        _throttle_delay_ms = max(_throttle_delay_ms // 2, 0)


def _get_credential():
    global _credential
    if _credential is None:
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if client_id:
            _credential = ManagedIdentityCredential(client_id=client_id)
        else:
            _credential = DefaultAzureCredential()
    return _credential


async def get_cosmos_client(config: Dict[str, Any]) -> CosmosClient:
    """Get CosmosDB client from config (cached per endpoint)."""
    endpoint = config["endpoint"]
    if endpoint not in _client_cache:
        _client_cache[endpoint] = CosmosClient(
            endpoint,
            credential=_get_credential(),
            retry_total=9,
            retry_backoff_max=30,
            retry_backoff_factor=0.5,
        )
    return _client_cache[endpoint]


async def _retry_on_429(func, *args, **kwargs):
    """Retry a sync function on 429 - never give up on rate limits."""
    await _adaptive_throttle()  # Apply current throttle before trying

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = func(*args, **kwargs)
            await _decrease_throttle()  # Success - reduce throttle
            return result
        except CosmosHttpResponseError as e:
            if e.status_code == 429:
                await _increase_throttle()  # Slow down globally
                # Get retry-after from headers or use exponential backoff
                retry_after_ms = int(e.headers.get("x-ms-retry-after-ms", min(BASE_DELAY_MS * (2 ** attempt), MAX_DELAY_MS)))
                logger.warning(f"429 on attempt {attempt}/{MAX_RETRIES}, retry after {retry_after_ms}ms, throttle={_throttle_delay_ms}ms")
                await asyncio.sleep(retry_after_ms / 1000.0)
                if attempt >= MAX_RETRIES:
                    # Even after max retries, try one more time after longer wait
                    logger.error(f"429 persists after {MAX_RETRIES} attempts, final retry after 60s")
                    await asyncio.sleep(60)
                    return func(*args, **kwargs)  # Last attempt
            else:
                raise


async def test_vector_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    """Test CosmosDB vector destination connection."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    # Get container properties
    props = container.read()

    # Check for vector index
    indexing_policy = props.get("indexingPolicy", {})
    vector_indexes = indexing_policy.get("vectorIndexes", [])
    vector_embedding_policy = props.get("vectorEmbeddingPolicy", {})
    vector_embeddings = vector_embedding_policy.get("vectorEmbeddings", [])

    # Build structured vector index details
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

    # Extract partition key path
    partition_key_def = props.get("partitionKey", {})
    pk_paths = partition_key_def.get("paths", [])
    partition_key_path = pk_paths[0] if pk_paths else None

    # Extract vector field from embedding policy
    vector_field = None
    if vector_embeddings:
        vector_field = vector_embeddings[0].get("path", "").lstrip("/") or None

    return {
        "status": "connected",
        "database": config["database"],
        "container": config["container"],
        "partition_key_path": partition_key_path,
        "vector_field": vector_field,
        "vector_indexes": structured_indexes,
        "has_vector_policy": len(vector_indexes) > 0
    }


async def probe_container_config(config: Dict[str, Any]) -> Dict[str, str]:
    """Probe a CosmosDB container and return partition_key_path and vector_field."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])
    props = container.read()

    result = {}

    # Partition key
    pk_paths = props.get("partitionKey", {}).get("paths", [])
    if pk_paths:
        result["partition_key_path"] = pk_paths[0]

    # Vector field from embedding policy
    vector_embeddings = props.get("vectorEmbeddingPolicy", {}).get("vectorEmbeddings", [])
    if vector_embeddings:
        vf = vector_embeddings[0].get("path", "").lstrip("/")
        if vf:
            result["vector_field"] = vf

    return result


async def write_vector(
    config: Dict[str, Any],
    doc_id: str,
    embedding: List[float],
    metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """Write vector embedding to CosmosDB."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    vector_field = config.get("vector_field", "embedding")
    id_field = config.get("id_field", "id")

    # Flatten embedding if nested
    flat_embedding = embedding
    if embedding and isinstance(embedding[0], list):
        flat_embedding = embedding[0]

    document = {
        id_field: doc_id,
        vector_field: flat_embedding,
        **metadata
    }

    result = await _retry_on_429(container.upsert_item, document)

    return {
        "id": result.get("id"),
        "etag": result.get("_etag")
    }


async def patch_vector_inplace(
    config: Dict[str, Any],
    doc_id: str,
    embedding: List[float],
    attrs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Patch an existing document: set embedding + optional attributes via CosmosDB patch."""
    from datetime import datetime

    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    vector_field = config.get("vector_field", "embedding")

    # Get partition key path - query container if not in config
    partition_key_path = config.get("partition_key_path")
    if not partition_key_path:
        try:
            props = container.read()
            pk_paths = props.get("partitionKey", {}).get("paths", [])
            partition_key_path = pk_paths[0] if pk_paths else "/id"
        except Exception:
            partition_key_path = "/id"
    pk_field = partition_key_path.lstrip("/")

    # Flatten embedding if nested
    flat_embedding = embedding
    if embedding and isinstance(embedding[0], list):
        flat_embedding = embedding[0]

    # Resolve partition key value
    if pk_field == "id":
        pk_value = doc_id
    else:
        rows = list(container.query_items(
            "SELECT c.id, c.{} FROM c WHERE c.id = @id".format(pk_field),
            parameters=[{"name": "@id", "value": doc_id}],
            enable_cross_partition_query=True,
        ))
        if not rows:
            raise ValueError(f"Document '{doc_id}' not found for in-place patch")
        pk_value = rows[0].get(pk_field)

    # Build patch operations
    ops = [
        {"op": "set", "path": f"/{vector_field}", "value": flat_embedding},
        {"op": "set", "path": "/embedded_at", "value": datetime.utcnow().isoformat()},
        {"op": "set", "path": "/embedding_dims", "value": len(flat_embedding)},
    ]
    for key, value in (attrs or {}).items():
        ops.append({"op": "set", "path": f"/{key}", "value": value})

    result = await _retry_on_429(container.patch_item, item=doc_id, partition_key=pk_value, patch_operations=ops)

    return {
        "id": result.get("id"),
        "etag": result.get("_etag"),
    }


async def write_vector_chunks(
    config: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Write multiple chunk vector documents to CosmosDB (upsert for idempotency)."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])
    vector_field = config.get("vector_field", "embedding")
    id_field = config.get("id_field", "id")

    results = []
    for chunk in chunks:
        flat = chunk["embedding"]
        if flat and isinstance(flat[0], list):
            flat = flat[0]

        doc = {id_field: chunk["id"], vector_field: flat}
        for k, v in chunk.items():
            if k not in ("id", "embedding"):
                doc[k] = v

        result = await _retry_on_429(container.upsert_item, doc)
        results.append({"id": result.get("id"), "etag": result.get("_etag")})

    return results


async def delete_chunks_by_prefix(
    config: Dict[str, Any],
    prefix: str,
) -> int:
    """Delete all vector documents whose ID starts with the given prefix.
    Used to clean up old chunks when re-processing a document."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    # Resolve partition key field from config or container properties
    pk_path = config.get("partition_key_path", "")
    if not pk_path:
        props = container.read()
        pk_paths = props.get("partitionKey", {}).get("paths", [])
        pk_path = pk_paths[0] if pk_paths else "/id"
    pk_field = pk_path.lstrip("/")

    # Query must include the partition key field for deletes
    if pk_field == "id":
        query = "SELECT c.id FROM c WHERE STARTSWITH(c.id, @prefix)"
    else:
        query = f"SELECT c.id, c.{pk_field} FROM c WHERE STARTSWITH(c.id, @prefix)"

    rows = list(container.query_items(
        query,
        parameters=[{"name": "@prefix", "value": prefix}],
        enable_cross_partition_query=True,
    ))

    deleted = 0
    for row in rows:
        try:
            pk_value = row.get(pk_field, row["id"])
            container.delete_item(row["id"], partition_key=pk_value)
            deleted += 1
        except Exception:
            pass
    return deleted


async def search_vectors(
    config: Dict[str, Any],
    query_vector: List[float],
    top_k: int = 10
) -> List[Dict[str, Any]]:
    """Search for similar vectors."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    vector_field = config.get("vector_field", "embedding")

    # CosmosDB vector search query
    query = f"""
    SELECT TOP {top_k} c.id, c.source, c.source_ref,
           VectorDistance(c.{vector_field}, @queryVector) AS score
    FROM c
    ORDER BY VectorDistance(c.{vector_field}, @queryVector)
    """

    results = list(container.query_items(
        query,
        parameters=[{"name": "@queryVector", "value": query_vector}],
        enable_cross_partition_query=True
    ))

    return results
