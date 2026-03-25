"""Azure Key Vault client for secure model API key storage.

Keys are cached in memory with a 5-minute TTL to minimize Key Vault calls.
Falls back gracefully when Key Vault is not configured (local dev).
"""

import os
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes
_cache: dict[str, tuple[str, float]] = {}  # {secret_name: (value, expires_at)}
_cache_lock = threading.Lock()
_client = None
_initialized = False


def _get_client():
    """Lazy-init the SecretClient. Returns None if Key Vault not configured."""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    vault_uri = os.getenv("KEY_VAULT_URI", "")
    if not vault_uri:
        logger.info("KEY_VAULT_URI not set — model API keys will be stored in CosmosDB (not recommended for production)")
        return None

    try:
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential

        client_id = os.getenv("AZURE_CLIENT_ID")
        credential = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id else DefaultAzureCredential()
        _client = SecretClient(vault_url=vault_uri, credential=credential)
        logger.info("Key Vault client initialized: %s", vault_uri)
    except Exception as e:
        logger.warning("Failed to initialize Key Vault client: %s — falling back to CosmosDB storage", e)
        _client = None

    return _client


def _secret_name(model_id: str) -> str:
    """Convert model ID to a valid Key Vault secret name."""
    # Key Vault allows alphanumerics and hyphens, max 127 chars
    return f"model-apikey-{model_id}"


def set_model_api_key(model_id: str, api_key: str) -> bool:
    """Store a model's API key in Key Vault. Returns True if stored, False if fallback."""
    client = _get_client()
    if not client:
        return False

    name = _secret_name(model_id)
    try:
        client.set_secret(name, api_key)
        with _cache_lock:
            _cache[name] = (api_key, time.time() + _CACHE_TTL)
        logger.info("Stored API key for model %s in Key Vault", model_id)
        return True
    except Exception as e:
        logger.error("Failed to store API key in Key Vault for model %s: %s", model_id, e)
        return False


def get_model_api_key(model_id: str) -> Optional[str]:
    """Retrieve a model's API key from Key Vault (cached). Returns None if not found."""
    client = _get_client()
    if not client:
        return None

    name = _secret_name(model_id)

    # Check cache first
    with _cache_lock:
        if name in _cache:
            value, expires_at = _cache[name]
            if time.time() < expires_at:
                return value

    # Fetch from Key Vault
    try:
        secret = client.get_secret(name)
        value = secret.value
        with _cache_lock:
            _cache[name] = (value, time.time() + _CACHE_TTL)
        return value
    except Exception as e:
        logger.warning("Failed to get API key from Key Vault for model %s: %s", model_id, e)
        return None


def delete_model_api_key(model_id: str):
    """Delete a model's API key from Key Vault."""
    client = _get_client()
    if not client:
        return

    name = _secret_name(model_id)
    with _cache_lock:
        _cache.pop(name, None)

    try:
        client.begin_delete_secret(name)
        logger.info("Deleted API key for model %s from Key Vault", model_id)
    except Exception as e:
        logger.warning("Failed to delete API key from Key Vault for model %s: %s", model_id, e)
