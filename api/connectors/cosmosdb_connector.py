"""CosmosDB Source Connector"""

import os
import hashlib
from typing import List, Dict, Any
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential, DefaultAzureCredential


class SkipDocument(Exception):
    """Raised when a document should be skipped (e.g. already has embedding)."""
    pass


# Cache: endpoint → CosmosClient (reuse connections + tokens)
_client_cache: Dict[str, CosmosClient] = {}
_credential = None


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
        _client_cache[endpoint] = CosmosClient(endpoint, credential=_get_credential())
    return _client_cache[endpoint]


async def test_cosmosdb_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    """Test CosmosDB connection."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    # Get container properties
    props = container.read()

    # Count documents
    query = "SELECT VALUE COUNT(1) FROM c"
    count = list(container.query_items(query, enable_cross_partition_query=True))[0]

    return {
        "status": "connected",
        "database": config["database"],
        "container": config["container"],
        "document_count": count
    }


async def list_documents(config: Dict[str, Any], full_sync: bool = False) -> List[Dict[str, Any]]:
    """List documents in container."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    query = config.get("query", "SELECT * FROM c")
    content_field = config.get("content_field", "content")

    documents = []
    for item in container.query_items(query, enable_cross_partition_query=True):
        documents.append({
            "ref": item.get("id"),
            "metadata": {
                "id": item.get("id"),
                "partition_key": item.get("_partitionKey"),
                "_ts": item.get("_ts")
            }
        })

    return documents


async def get_document(config: Dict[str, Any], doc_id: str) -> str:
    """Get document content. Raises SkipDocument if embedding already exists."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    content_field = config.get("content_field", "content")

    # Try to read document
    items = list(container.query_items(
        f"SELECT * FROM c WHERE c.id = '{doc_id}'",
        enable_cross_partition_query=True
    ))

    if not items:
        raise ValueError(f"Document '{doc_id}' not found")

    doc = items[0]

    # Support multiple content fields — concatenate in order
    content = _extract_content(doc, content_field)
    current_hash = hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()

    # Skip if embedding exists and content hasn't changed
    if doc.get("embedding") and doc.get("content_hash") == current_hash:
        raise SkipDocument(f"Document '{doc_id}' content unchanged (hash match)")

    return content, current_hash


def _extract_content(doc: dict, content_field) -> str:
    """Extract content from a document, supporting single or multiple fields."""
    if isinstance(content_field, list):
        parts = []
        for f in content_field:
            val = doc.get(f)
            if val and isinstance(val, str):
                parts.append(val)
        return "\n\n".join(parts) if parts else ""
    return doc.get(content_field, "")
