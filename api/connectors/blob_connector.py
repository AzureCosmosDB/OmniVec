"""Azure Blob Storage Connector"""

import os
from typing import List, Dict, Any, Optional, Tuple
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential


async def get_blob_client(config: Dict[str, Any]) -> BlobServiceClient:
    """Get blob service client from config."""
    if config.get("connection_string"):
        return BlobServiceClient.from_connection_string(config["connection_string"])
    elif config.get("account_url"):
        credential = DefaultAzureCredential()
        return BlobServiceClient(config["account_url"], credential=credential)
    else:
        raise ValueError("Either connection_string or account_url required")


async def test_blob_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    """Test blob storage connection."""
    client = await get_blob_client(config)
    container = client.get_container_client(config["container"])

    # Try to get container properties
    props = container.get_container_properties()

    # Count blobs
    count = 0
    for _ in container.list_blobs(name_starts_with=config.get("prefix", ""), results_per_page=10):
        count += 1
        if count >= 10:
            break

    return {
        "status": "connected",
        "container": config["container"],
        "sample_count": count,
        "last_modified": str(props.last_modified)
    }


async def list_blobs(config: Dict[str, Any], full_sync: bool = False) -> List[Dict[str, Any]]:
    """List blobs in container matching filters."""
    client = await get_blob_client(config)
    container = client.get_container_client(config["container"])

    # Support both new file_types and legacy extensions
    file_types = config.get("file_types", [])
    extensions = config.get("extensions", [])

    # Convert file_types to extensions format if provided
    if file_types:
        allowed_extensions = set(f".{ft.lstrip('.')}" for ft in file_types)
    elif extensions:
        allowed_extensions = set(extensions)
    else:
        allowed_extensions = {".pdf", ".txt", ".json", ".md", ".csv"}

    prefix = config.get("prefix", "")

    documents = []

    for blob in container.list_blobs(name_starts_with=prefix):
        # Check extension
        ext = os.path.splitext(blob.name)[1].lower()
        if allowed_extensions and ext not in allowed_extensions:
            continue

        documents.append({
            "ref": blob.name,
            "metadata": {
                "filename": os.path.basename(blob.name),
                "size": blob.size,
                "content_type": blob.content_settings.content_type if blob.content_settings else None,
                "last_modified": str(blob.last_modified),
                "etag": blob.etag,
                "file_type": ext.lstrip('.')
            }
        })

    return documents


async def list_blobs_paginated(
    config: Dict[str, Any],
    page_size: int = 1000,
    continuation_token: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """List blobs with pagination support for checkpointing.

    Args:
        config: Blob storage configuration
        page_size: Number of blobs per page
        continuation_token: Token from previous call to resume enumeration

    Returns:
        Tuple of (list of blob documents, next continuation token or None if done)
    """
    client = await get_blob_client(config)
    container = client.get_container_client(config["container"])

    # Support both new file_types and legacy extensions
    file_types = config.get("file_types", [])
    extensions = config.get("extensions", [])

    if file_types:
        allowed_extensions = set(f".{ft.lstrip('.')}" for ft in file_types)
    elif extensions:
        allowed_extensions = set(extensions)
    else:
        allowed_extensions = {".pdf", ".txt", ".json", ".md", ".csv"}

    prefix = config.get("prefix", "")

    documents = []
    next_token = None

    # Use by_page() for paginated access with continuation support
    pages = container.list_blobs(
        name_starts_with=prefix,
        results_per_page=page_size
    ).by_page(continuation_token=continuation_token)

    # Get one page
    try:
        page = next(pages)
        for blob in page:
            ext = os.path.splitext(blob.name)[1].lower()
            if allowed_extensions and ext not in allowed_extensions:
                continue

            documents.append({
                "ref": blob.name,
                "metadata": {
                    "filename": os.path.basename(blob.name),
                    "size": blob.size,
                    "content_type": blob.content_settings.content_type if blob.content_settings else None,
                    "last_modified": str(blob.last_modified),
                    "etag": blob.etag,
                    "file_type": ext.lstrip('.')
                }
            })

        # Get continuation token for next page
        next_token = pages.continuation_token
    except StopIteration:
        # No more pages
        pass

    return documents, next_token


async def download_blob(config: Dict[str, Any], blob_name: str) -> bytes:
    """Download blob content."""
    client = await get_blob_client(config)
    container = client.get_container_client(config["container"])
    blob = container.get_blob_client(blob_name)

    return blob.download_blob().readall()
