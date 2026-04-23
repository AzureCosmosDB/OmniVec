"""OmniVec CosmosDB Metadata Store

Single-container persistence layer for all control plane state.
Container: 'metadata', partition key: '/doc_type'
"""
from __future__ import annotations

import os
import logging
from typing import Any, Optional

from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos.exceptions import (
    CosmosResourceNotFoundError,
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
)
from azure.identity import DefaultAzureCredential

from cosmos_retry import cosmos_retry

logger = logging.getLogger(__name__)

DATABASE_NAME = "omnivec"
CONTAINER_NAME = "metadata"


class MetadataStore:
    """Thin wrapper around CosmosDB for control plane state.

    All public methods retry automatically on transient errors (429/408/5xx)
    with server-directed backoff; semantic errors (NotFound / ResourceExists /
    EtagMismatch) are raised immediately so callers can react.
    """

    def __init__(self, endpoint: str, key: str = None):
        if key:
            self._client = CosmosClient(endpoint, credential=key)
            logger.info("MetadataStore using key-based auth")
        else:
            credential = DefaultAzureCredential()
            self._client = CosmosClient(endpoint, credential=credential)
            logger.info("MetadataStore using DefaultAzureCredential")
        self._database = self._client.get_database_client(DATABASE_NAME)
        self._container = self._database.get_container_client(CONTAINER_NAME)
        logger.info("MetadataStore initialized: %s / %s / %s", endpoint, DATABASE_NAME, CONTAINER_NAME)

    @cosmos_retry()
    def get(self, doc_id: str, partition_key: str) -> Optional[dict]:
        """Read a single document by id and partition key (doc_type).

        Returns None if the document does not exist.
        """
        try:
            return self._container.read_item(item=doc_id, partition_key=partition_key)
        except CosmosResourceNotFoundError:
            return None

    @cosmos_retry()
    def create(self, doc: dict) -> dict:
        """Create a new document. Fails if document already exists.

        Must contain 'id' and 'doc_type' fields.
        Raises CosmosResourceExistsError if document already exists.
        """
        return self._container.create_item(doc)

    @cosmos_retry()
    def list(self, doc_type: str) -> list[dict]:
        """Query all documents of a given type."""
        query = "SELECT * FROM c WHERE c.doc_type = @doc_type"
        params = [{"name": "@doc_type", "value": doc_type}]
        return list(self._container.query_items(
            query=query,
            parameters=params,
            partition_key=doc_type,
        ))

    @cosmos_retry()
    def upsert(self, doc: dict) -> dict:
        """Upsert a document. Must contain 'id' and 'doc_type' fields."""
        return self._container.upsert_item(doc)

    @cosmos_retry()
    def delete(self, doc_id: str, partition_key: str) -> None:
        """Delete a document by id and partition key (doc_type)."""
        self._container.delete_item(item=doc_id, partition_key=partition_key)

    def get_container(self):
        """Return the raw CosmosDB container client (for Change Feed, etc.)."""
        return self._container

    @cosmos_retry()
    def replace_with_etag(self, doc: dict, etag: str) -> dict:
        """Replace a document with optimistic concurrency via etag.

        Raises CosmosAccessConditionFailedError if the document was modified
        since the etag was read (another worker claimed it).
        """
        return self._container.replace_item(
            item=doc["id"],
            body=doc,
            if_match=etag,
        )

    @cosmos_retry()
    def query(self, query: str, parameters: list = None, partition_key: str = None) -> list[dict]:
        """Run a parameterized query."""
        kwargs = {"query": query, "parameters": parameters or []}
        if partition_key is not None:
            kwargs["partition_key"] = partition_key
        else:
            kwargs["enable_cross_partition_query"] = True
        return list(self._container.query_items(**kwargs))


# Module-level singleton
_store: Optional[MetadataStore] = None


def init_store() -> MetadataStore:
    """Initialize the global store from environment. Call once on startup."""
    global _store
    endpoint = os.environ.get("COSMOS_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT environment variable is required")
    key = os.environ.get("COSMOS_KEY", "")
    _store = MetadataStore(endpoint, key=key if key else None)
    return _store


def get_store() -> MetadataStore:
    """Return the initialized store singleton."""
    if _store is None:
        raise RuntimeError("Store not initialized. Call init_store() first.")
    return _store
