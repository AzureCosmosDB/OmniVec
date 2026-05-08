"""Pluggable model persistence for DocGrok.

Storage backends:
  - memory:   In-memory only (default, no persistence)
  - cosmosdb: Azure CosmosDB with managed identity

Configuration via environment variables:
  MODEL_STORE_TYPE=cosmosdb
  COSMOS_ENDPOINT=https://omnivec-cosmos.documents.azure.com:443/
  COSMOS_DATABASE=omnivec
  COSMOS_CONTAINER=metadata
"""

import os
from abc import ABC, abstractmethod


class ModelStore(ABC):
    """Abstract interface for model persistence."""

    @abstractmethod
    def list_models(self) -> dict[str, dict]:
        """Return all external models as {model_id: config}."""

    @abstractmethod
    def get_model(self, model_id: str) -> dict | None:
        """Get a single model config by ID, or None."""

    @abstractmethod
    def upsert_model(self, model_id: str, config: dict) -> None:
        """Create or update a model."""

    @abstractmethod
    def delete_model(self, model_id: str) -> bool:
        """Delete a model. Returns True if it existed."""


class InMemoryStore(ModelStore):
    """No persistence — models lost on restart."""

    def list_models(self) -> dict[str, dict]:
        return {}

    def get_model(self, model_id: str) -> dict | None:
        return None

    def upsert_model(self, model_id: str, config: dict) -> None:
        pass

    def delete_model(self, model_id: str) -> bool:
        return False


class CosmosDBStore(ModelStore):
    """Persists models as doc_type='docgrok_model' in CosmosDB metadata container."""

    def __init__(self, endpoint: str, database: str, container: str):
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
        client = CosmosClient(endpoint, DefaultAzureCredential())
        self._container = client.get_database_client(database).get_container_client(container)

    def list_models(self) -> dict[str, dict]:
        docs = list(self._container.query_items(
            "SELECT * FROM c WHERE c.doc_type = 'docgrok_model'",
            partition_key="docgrok_model"
        ))
        result = {}
        for doc in docs:
            model_id = doc["id"]
            cfg = {k: v for k, v in doc.items()
                   if k not in ("doc_type", "stored_at") and not k.startswith("_")}
            cfg.pop("id", None)
            result[model_id] = cfg
        return result

    def get_model(self, model_id: str) -> dict | None:
        try:
            doc = self._container.read_item(model_id, partition_key="docgrok_model")
            cfg = {k: v for k, v in doc.items()
                   if k not in ("doc_type", "stored_at") and not k.startswith("_")}
            cfg.pop("id", None)
            return cfg
        except Exception:
            return None

    def upsert_model(self, model_id: str, config: dict) -> None:
        from datetime import datetime
        doc = {
            "id": model_id,
            "doc_type": "docgrok_model",
            **{k: v for k, v in config.items() if k != "api_key"},
            "stored_at": datetime.utcnow().isoformat(),
        }
        self._container.upsert_item(doc)

    def delete_model(self, model_id: str) -> bool:
        try:
            self._container.delete_item(model_id, partition_key="docgrok_model")
            return True
        except Exception:
            return False


def create_store() -> ModelStore:
    """Factory — reads MODEL_STORE_TYPE env var."""
    store_type = os.getenv("MODEL_STORE_TYPE", "memory").lower()

    if store_type == "cosmosdb":
        endpoint = os.getenv("COSMOS_ENDPOINT", "")
        database = os.getenv("COSMOS_DATABASE", "omnivec")
        container = os.getenv("COSMOS_CONTAINER", "metadata")
        if not endpoint:
            print("WARNING: MODEL_STORE_TYPE=cosmosdb but COSMOS_ENDPOINT not set, falling back to memory")
            return InMemoryStore()
        try:
            store = CosmosDBStore(endpoint, database, container)
            print(f"DocGrok: CosmosDB model store initialized ({endpoint})")
            return store
        except Exception as e:
            print(f"WARNING: CosmosDB store init failed ({e}), falling back to memory")
            return InMemoryStore()

    return InMemoryStore()
