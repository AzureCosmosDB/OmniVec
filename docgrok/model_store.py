"""Pluggable model persistence for DocGrok.

Storage backends:
  - memory:   In-memory only (default, no persistence)
  - cosmosdb: Azure CosmosDB with managed identity

Configuration via environment variables:
  MODEL_STORE_TYPE=cosmosdb
  COSMOS_ENDPOINT=https://<your-cosmos-account>.documents.azure.com:443/
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
        from envelope_crypto import get_cipher, CipherError
        cipher = get_cipher()
        docs = list(self._container.query_items(
            "SELECT * FROM c WHERE c.doc_type = 'docgrok_model'",
            partition_key="docgrok_model"
        ))
        result = {}
        for doc in docs:
            model_id = doc["id"]
            cfg = {k: v for k, v in doc.items()
                   if k not in ("doc_type", "stored_at", "api_key_envelope")
                   and not k.startswith("_")}
            cfg.pop("id", None)
            envelope = doc.get("api_key_envelope") or ""
            if envelope:
                try:
                    cfg["api_key"] = cipher.decrypt(envelope)
                except CipherError as e:
                    print(f"WARNING: failed to decrypt api_key for {model_id}: {e}")
                    cfg["api_key"] = ""
            else:
                cfg.setdefault("api_key", "")
            result[model_id] = cfg
        return result

    def get_model(self, model_id: str) -> dict | None:
        from envelope_crypto import get_cipher, CipherError
        try:
            doc = self._container.read_item(model_id, partition_key="docgrok_model")
        except Exception:
            return None
        cipher = get_cipher()
        cfg = {k: v for k, v in doc.items()
               if k not in ("doc_type", "stored_at", "api_key_envelope")
               and not k.startswith("_")}
        cfg.pop("id", None)
        envelope = doc.get("api_key_envelope") or ""
        if envelope:
            try:
                cfg["api_key"] = cipher.decrypt(envelope)
            except CipherError as e:
                print(f"WARNING: failed to decrypt api_key for {model_id}: {e}")
                cfg["api_key"] = ""
        else:
            cfg.setdefault("api_key", "")
        return cfg

    def upsert_model(self, model_id: str, config: dict) -> None:
        from datetime import datetime
        from envelope_crypto import get_cipher
        cipher = get_cipher()
        api_key = config.get("api_key") or ""
        envelope = ""
        if api_key:
            envelope = cipher.encrypt(api_key)
        # Preserve existing envelope if caller updates without a new key,
        # UNLESS _clear_api_key is set (explicit revoke).
        clear = bool(config.get("_clear_api_key"))
        if not api_key and not clear:
            try:
                existing = self._container.read_item(model_id, partition_key="docgrok_model")
                envelope = existing.get("api_key_envelope", "")
            except Exception:
                envelope = ""

        doc = {
            "id": model_id,
            "doc_type": "docgrok_model",
            **{k: v for k, v in config.items() if k not in ("api_key", "_clear_api_key")},
            "api_key_envelope": envelope,
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
