#!/usr/bin/env python3
"""Checkpoint Manager - Durable checkpoint storage with atomic updates.

Provides crash-resilient checkpointing for all worker types.
All checkpoints stored in CosmosDB with etag-based optimistic concurrency.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
    CosmosResourceExistsError,
)

from store import get_store

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Manages checkpoints for blob and CosmosDB workers."""

    def __init__(self, source_id: str, worker_type: str, location: str = ""):
        """
        Args:
            source_id: The source being processed
            worker_type: "backfill", "live", or "changefeed"
            location: Optional location identifier (for multi-location sources)
        """
        self.source_id = source_id
        self.worker_type = worker_type
        self.location = location
        self.checkpoint_id = self._build_checkpoint_id()
        self._current_etag: Optional[str] = None
        self._checkpoint: Optional[Dict[str, Any]] = None

    def _build_checkpoint_id(self) -> str:
        """Build unique checkpoint ID."""
        if self.location:
            # Hash location for consistent ID
            import hashlib
            loc_hash = hashlib.md5(self.location.encode()).hexdigest()[:8]
            return f"cp-{self.source_id}-{self.worker_type}-{loc_hash}"
        return f"cp-{self.source_id}-{self.worker_type}"

    def load(self) -> Optional[Dict[str, Any]]:
        """Load checkpoint from CosmosDB. Returns None if not exists."""
        store = get_store()
        try:
            doc = store.get(self.checkpoint_id, partition_key="checkpoint")
            self._current_etag = doc.get("_etag")
            self._checkpoint = {
                k: v for k, v in doc.items() if not k.startswith("_")
            }
            logger.info(
                "Loaded checkpoint %s: items_processed=%s, continuation=%s",
                self.checkpoint_id,
                self._checkpoint.get("state", {}).get("items_processed", 0),
                bool(self._checkpoint.get("state", {}).get("continuation_token"))
            )
            return self._checkpoint
        except CosmosResourceNotFoundError:
            logger.info("No existing checkpoint for %s", self.checkpoint_id)
            return None

    def save(
        self,
        continuation_token: Optional[str],
        last_item: str,
        items_processed: int,
        items_since_checkpoint: int = 0,
        extra_state: Optional[Dict[str, Any]] = None,
        extra_stats: Optional[Dict[str, Any]] = None,
        pod_name: Optional[str] = None,
    ) -> bool:
        """
        Save checkpoint atomically using etag.

        Returns True if saved successfully, False if concurrent update detected.
        """
        store = get_store()

        checkpoint = {
            "id": self.checkpoint_id,
            "doc_type": "checkpoint",
            "source_id": self.source_id,
            "worker_type": self.worker_type,
            "location": self.location,
            "state": {
                "continuation_token": continuation_token,
                "last_item_processed": last_item,
                "items_processed": items_processed,
                "items_since_checkpoint": items_since_checkpoint,
                **(extra_state or {})
            },
            "stats": {
                "updated_at": datetime.utcnow().isoformat(),
                **(extra_stats or {})
            },
            "worker": {
                "pod_name": pod_name or "",
                "updated_at": datetime.utcnow().isoformat(),
            },
            "updated_at": datetime.utcnow().isoformat(),
        }

        try:
            if self._current_etag:
                # Update with etag check
                store.replace_with_etag(checkpoint, self._current_etag)
            else:
                # First save - try create first to avoid race condition
                # If another worker already created it, we'll catch the error and load
                try:
                    store.create(checkpoint)
                except CosmosResourceExistsError:
                    # Another worker created it first - reload and retry with etag
                    logger.info("Checkpoint %s already exists, reloading", self.checkpoint_id)
                    self.load()
                    if self._current_etag:
                        store.replace_with_etag(checkpoint, self._current_etag)
                    else:
                        # Still no etag means load failed - don't overwrite
                        return False

            # Update local etag
            doc = store.get(self.checkpoint_id, partition_key="checkpoint")
            self._current_etag = doc.get("_etag")
            self._checkpoint = checkpoint

            logger.debug(
                "Saved checkpoint %s: items=%d, last=%s",
                self.checkpoint_id, items_processed, last_item[:50] if last_item else ""
            )
            return True

        except CosmosAccessConditionFailedError:
            logger.warning(
                "Checkpoint conflict for %s - another worker updated it",
                self.checkpoint_id
            )
            # Reload to get latest
            self.load()
            return False
        except Exception as e:
            logger.error("Failed to save checkpoint %s: %s", self.checkpoint_id, e)
            raise

    def reset(self) -> bool:
        """Reset checkpoint to start from beginning."""
        store = get_store()
        try:
            store.delete(self.checkpoint_id, partition_key="checkpoint")
            self._current_etag = None
            self._checkpoint = None
            logger.info("Reset checkpoint %s", self.checkpoint_id)
            return True
        except CosmosResourceNotFoundError:
            return True
        except Exception as e:
            logger.error("Failed to reset checkpoint %s: %s", self.checkpoint_id, e)
            raise

    def get_continuation_token(self) -> Optional[str]:
        """Get continuation token from loaded checkpoint."""
        if not self._checkpoint:
            return None
        return self._checkpoint.get("state", {}).get("continuation_token")

    def get_items_processed(self) -> int:
        """Get items processed count from loaded checkpoint."""
        if not self._checkpoint:
            return 0
        return self._checkpoint.get("state", {}).get("items_processed", 0)

    def get_last_item(self) -> Optional[str]:
        """Get last processed item from loaded checkpoint."""
        if not self._checkpoint:
            return None
        return self._checkpoint.get("state", {}).get("last_item_processed")


def get_all_checkpoints(source_id: Optional[str] = None) -> list:
    """Get all checkpoints, optionally filtered by source."""
    store = get_store()
    if source_id:
        query = (
            "SELECT * FROM c WHERE c.doc_type = 'checkpoint' "
            "AND c.source_id = @source_id"
        )
        params = [{"name": "@source_id", "value": source_id}]
    else:
        query = "SELECT * FROM c WHERE c.doc_type = 'checkpoint'"
        params = []

    return store.query(query, params, partition_key="checkpoint")


def delete_source_checkpoints(source_id: str) -> int:
    """Delete all checkpoints for a source. Returns count deleted."""
    store = get_store()
    checkpoints = get_all_checkpoints(source_id)
    deleted = 0
    for cp in checkpoints:
        try:
            store.delete(cp["id"], partition_key="checkpoint")
            deleted += 1
        except Exception:
            pass
    logger.info("Deleted %d checkpoints for source %s", deleted, source_id)
    return deleted
