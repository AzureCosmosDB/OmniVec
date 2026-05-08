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
    props = container.read()  # lgtm[py/unused-local-variable]

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


async def get_document(config: Dict[str, Any], doc_id: str, content_fields: list = None) -> str:
    """Get document content. Raises SkipDocument if embedding already exists."""
    client = await get_cosmos_client(config)
    database = client.get_database_client(config["database"])
    container = database.get_container_client(config["container"])

    if content_fields is None:
        content_fields = ["content"]

    # Try to read document
    items = list(container.query_items(
        f"SELECT * FROM c WHERE c.id = '{doc_id}'",
        enable_cross_partition_query=True
    ))

    if not items:
        raise ValueError(f"Document '{doc_id}' not found")

    doc = items[0]

    # Support multiple content fields — concatenate in order
    content = _extract_content(doc, content_fields)
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


import re
from urllib.parse import urlparse, unquote


def _extract_extension(s: str):
    if not s:
        return None
    clean = s.split("?", 1)[0].split("#", 1)[0]
    if "." not in clean:
        return None
    last_slash = max(clean.rfind("/"), clean.rfind("\\"))
    last_dot = clean.rfind(".")
    if last_dot < last_slash:
        return None
    ext = clean[last_dot + 1:].lower()
    return ext or None


def _resolve_blob_location(url: str, source_account_url: str = "", source_container: str = ""):
    """Resolve an attachment URL to (account_url, container, blob_name).

    Returns (None, None, "") for invalid / disallowed (non-Azure-Blob) URLs.
    SSRF guard: when ``source_account_url`` is set, only URLs whose host matches
    that account are accepted.
    """
    if not url:
        return None, None, ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        if parsed.scheme != "https":
            return None, None, ""
        if not parsed.netloc.lower().endswith(".blob.core.windows.net"):
            return None, None, ""
        if source_account_url:
            pinned = urlparse(source_account_url)
            if pinned.netloc.lower() != parsed.netloc.lower():
                return None, None, ""
        path = parsed.path.lstrip("/")
        if "/" not in path:
            return None, None, ""
        ctnr, blob = path.split("/", 1)
        if not blob:
            return None, None, ""
        return f"https://{parsed.netloc}", ctnr, unquote(blob)

    # Relative path
    if not source_account_url or not source_container:
        return None, None, ""
    return source_account_url, source_container, url.lstrip("/")


def _extract_attachments(doc: dict, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Iterate the document's attachments array (named by ``attachments_field``)
    and return entries that pass all configured filters.

    Mirrors ``Source.ExtractAttachments`` in the .NET watcher; used by the API
    layer for config validation and ad-hoc preview, while live ingestion runs
    in the .NET ChangeFeed worker.
    """
    field = config.get("attachments_field")
    if not field:
        return []
    arr = doc.get(field)
    if not isinstance(arr, list):
        return []

    name_field = config.get("attachment_name_field", "name")
    url_field = config.get("attachment_url_field", "url")
    ct_field = config.get("attachment_content_type_field", "contentType")

    name_re = config.get("attachment_name_regex")
    name_pat = re.compile(name_re, re.IGNORECASE) if name_re else None

    file_types = config.get("attachment_file_types") or []
    if isinstance(file_types, str):
        file_types = [t.strip() for t in file_types.split(",") if t.strip()]
    file_types = {t.lstrip(".").lower() for t in file_types}

    content_types = config.get("attachment_content_types") or []
    if isinstance(content_types, str):
        content_types = [t.strip() for t in content_types.split(",") if t.strip()]
    content_types = {t.lower() for t in content_types}

    src_account = config.get("account_url", "")
    # For cosmosdb attachment-mode sources, "container" names the cosmos
    # container; honor "attachment_blob_container" as the dedicated key for
    # the default blob container used to resolve relative attachment URLs.
    src_container = config.get("attachment_blob_container") or config.get("container", "")

    matches: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = str(item.get(name_field, "") or "")
        url = str(item.get(url_field, "") or "")
        ctype = item.get(ct_field)
        if not url:
            continue
        if name_pat and not name_pat.search(name):
            continue
        if file_types:
            ext = _extract_extension(name) or _extract_extension(url)
            if not ext or ext not in file_types:
                continue
        if content_types:
            if not ctype or str(ctype).lower() not in content_types:
                continue
        acct, ctnr, blob = _resolve_blob_location(url, src_account, src_container)
        if not blob:
            continue
        matches.append({
            "name": name or blob,
            "url": url,
            "content_type": ctype,
            "blob_account_url": acct,
            "blob_container": ctnr,
            "blob_name": blob,
        })
    return matches
