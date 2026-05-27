"""Envelope encryption for sensitive per-model secrets (api_keys).

Why envelope encryption (and not "just put it in Key Vault" / "just put it in
a k8s Secret" / "just store ciphertext in Cosmos"):

  * 10k+ models: a single Key Vault holds at most ~25k secrets and is rate
    limited to ~2k ops/sec. Storing each api_key as a KV secret puts the hot
    embed path one network hop away from a throttled, shared resource.
  * k8s Secrets don't scale across cluster boundaries, can't be rotated via
    API without a pod roll, and tie persistence to the orchestrator.
  * Plain ciphertext in Cosmos with no KMS gives no rotation story and no
    way to crypto-shred a tenant.

Design:
  * Key Vault holds ONE long-lived Key Encryption Key (KEK), HSM-backed and
    audited. Default name: `docgrok-model-kek`.
  * Per call to `encrypt()` we generate a fresh 32-byte Data Encryption Key
    (DEK), encrypt the plaintext with AES-256-GCM, then wrap the DEK with
    the KEK (RSA-OAEP-256) and store the wrapped DEK alongside the
    ciphertext.
  * Decryption unwraps the DEK via the KEK once per (model, kek_version)
    and caches the plaintext DEK in-process behind a TTL (default 1h). The
    hot embed path is therefore a local AES-GCM decrypt (microseconds) and
    never touches Key Vault.
  * Rotating the api_key is a single Cosmos write — generate a new envelope
    with the same KEK and overwrite the doc.
  * Rotating the KEK is opaque to data: Key Vault auto-rotates, future
    writes use the new version, old envelopes still decrypt with the old
    version (we record `kek_version` per envelope).
  * Crypto-shredding a tenant: delete/disable the KEK version they used in
    Key Vault — all envelopes wrapped with it become permanently
    undecryptable.

Configuration (env vars):
  KEY_VAULT_URI          Vault URI (required for envelope mode).
  MODEL_KEK_NAME         KEK key name. Default: docgrok-model-kek.
  AZURE_CLIENT_ID        Workload-identity client id. Optional.
  MODEL_DEK_CACHE_TTL    DEK cache TTL in seconds. Default: 3600.

Fallback:
  When KEY_VAULT_URI is unset the module returns a `NullCipher` which
  preserves plaintext (only for local dev / unit tests). Production
  deployments MUST set KEY_VAULT_URI.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Magic header so we can tell envelope blobs apart from anything else that
# might one day end up in the same field (plaintext, base64, etc.).
ENVELOPE_VERSION = 1
ENVELOPE_PREFIX = "ev1:"
_KEK_WRAP_ALGO = "RSA-OAEP-256"


@dataclass(frozen=True)
class Envelope:
    """Serializable envelope structure (everything below is non-secret)."""
    version: int
    nonce_b64: str
    ciphertext_b64: str
    dek_wrapped_b64: str
    kek_version: str  # the Key Vault key version used to wrap the DEK

    def serialize(self) -> str:
        payload = {
            "v": self.version,
            "n": self.nonce_b64,
            "c": self.ciphertext_b64,
            "w": self.dek_wrapped_b64,
            "k": self.kek_version,
            "a": _KEK_WRAP_ALGO,
        }
        return ENVELOPE_PREFIX + base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    @classmethod
    def parse(cls, blob: str) -> "Envelope":
        if not blob.startswith(ENVELOPE_PREFIX):
            raise ValueError("not an envelope blob")
        raw = base64.b64decode(blob[len(ENVELOPE_PREFIX):])
        d = json.loads(raw)
        return cls(
            version=int(d["v"]),
            nonce_b64=d["n"],
            ciphertext_b64=d["c"],
            dek_wrapped_b64=d["w"],
            kek_version=d["k"],
        )


class CipherError(Exception):
    """Raised for any envelope encryption/decryption failure."""


class Cipher:
    """Abstract API. Implementations must be thread-safe."""

    def encrypt(self, plaintext: str) -> str:
        raise NotImplementedError

    def decrypt(self, blob: str) -> str:
        raise NotImplementedError

    def is_envelope(self, value: Optional[str]) -> bool:
        return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


class NullCipher(Cipher):
    """No-op cipher for local dev / tests when KEY_VAULT_URI is unset."""

    def encrypt(self, plaintext: str) -> str:
        # Returned as-is; callers MUST treat the column as sensitive anyway.
        return plaintext

    def decrypt(self, blob: str) -> str:
        if self.is_envelope(blob):
            raise CipherError(
                "Received an envelope blob but no KEY_VAULT_URI is configured."
            )
        return blob


class EnvelopeCipher(Cipher):
    """Envelope encryption backed by Azure Key Vault Keys (KEK)."""

    def __init__(self, vault_uri: str, kek_name: str, dek_cache_ttl: int = 3600):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys import KeyClient
        from azure.keyvault.keys.crypto import CryptographyClient

        client_id = os.environ.get("AZURE_CLIENT_ID")
        cred = (
            DefaultAzureCredential(managed_identity_client_id=client_id)
            if client_id else DefaultAzureCredential()
        )
        self._credential = cred
        self._key_client = KeyClient(vault_url=vault_uri, credential=cred)
        self._kek_name = kek_name

        # Resolve the active KEK (creating one only if explicitly enabled).
        try:
            self._kek = self._key_client.get_key(kek_name)
        except Exception as e:
            if os.environ.get("MODEL_KEK_AUTOCREATE", "").lower() == "true":
                logger.warning(
                    "KEK %s not found; auto-creating (MODEL_KEK_AUTOCREATE=true)",
                    kek_name,
                )
                self._kek = self._key_client.create_rsa_key(kek_name, size=3072)
            else:
                raise CipherError(
                    f"KEK '{kek_name}' not found in {vault_uri} and "
                    f"MODEL_KEK_AUTOCREATE is not enabled."
                ) from e

        self._crypto = CryptographyClient(self._kek, credential=cred)
        self._dek_cache_ttl = dek_cache_ttl
        # cache: { (kek_version, dek_wrapped_b64) : (dek_bytes, expires_at) }
        self._dek_cache: dict[tuple[str, str], tuple[bytes, float]] = {}
        self._cache_lock = threading.Lock()

    # ---- public Cipher API -------------------------------------------------

    @property
    def kek_version(self) -> str:
        return self._kek.properties.version or ""

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise CipherError("plaintext must not be None")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek = secrets.token_bytes(32)              # AES-256 key
        nonce = secrets.token_bytes(12)            # 96-bit nonce (GCM standard)
        aesgcm = AESGCM(dek)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)

        wrap = self._crypto.wrap_key(_KEK_WRAP_ALGO, dek)
        env = Envelope(
            version=ENVELOPE_VERSION,
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            ciphertext_b64=base64.b64encode(ct).decode("ascii"),
            dek_wrapped_b64=base64.b64encode(wrap.encrypted_key).decode("ascii"),
            kek_version=self.kek_version,
        )
        return env.serialize()

    def decrypt(self, blob: str) -> str:
        if not self.is_envelope(blob):
            # Legacy data path: docs written before envelope encryption have
            # the raw api_key (or empty string). Return as-is — callers can
            # detect and migrate.
            return blob

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        env = Envelope.parse(blob)

        cache_key = (env.kek_version, env.dek_wrapped_b64)
        now = time.time()
        with self._cache_lock:
            cached = self._dek_cache.get(cache_key)
            if cached and cached[1] > now:
                dek = cached[0]
            else:
                dek = None

        if dek is None:
            wrapped = base64.b64decode(env.dek_wrapped_b64)
            crypto = self._crypto_for_version(env.kek_version)
            try:
                unwrap = crypto.unwrap_key(_KEK_WRAP_ALGO, wrapped)
            except Exception as e:
                raise CipherError(f"KEK unwrap failed: {e}") from e
            dek = unwrap.key
            with self._cache_lock:
                self._dek_cache[cache_key] = (dek, now + self._dek_cache_ttl)

        try:
            aesgcm = AESGCM(dek)
            pt = aesgcm.decrypt(
                base64.b64decode(env.nonce_b64),
                base64.b64decode(env.ciphertext_b64),
                associated_data=None,
            )
        except Exception as e:
            raise CipherError(f"AES-GCM decrypt failed: {e}") from e
        return pt.decode("utf-8")

    # ---- helpers -----------------------------------------------------------

    def _crypto_for_version(self, version: str):
        """Return a CryptographyClient bound to a specific KEK version.

        Required to decrypt envelopes wrapped by an older KEK version after a
        rotation.
        """
        from azure.keyvault.keys.crypto import CryptographyClient
        if not version or version == self.kek_version:
            return self._crypto
        # The versioned key id is <vault>/keys/<name>/<version>
        kid = f"{self._kek.id.rsplit('/', 1)[0]}/{version}"
        return CryptographyClient(kid, credential=self._credential)


# ---- module-level singleton --------------------------------------------------

_cipher: Optional[Cipher] = None
_cipher_lock = threading.Lock()


def get_cipher() -> Cipher:
    """Return the process-wide Cipher (envelope if KEY_VAULT_URI is set)."""
    global _cipher
    if _cipher is not None:
        return _cipher
    with _cipher_lock:
        if _cipher is not None:
            return _cipher

        vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
        kek_name = os.environ.get("MODEL_KEK_NAME", "docgrok-model-kek")
        ttl = int(os.environ.get("MODEL_DEK_CACHE_TTL", "3600"))

        if not vault_uri:
            logger.warning(
                "KEY_VAULT_URI is not set — model api_keys will be stored as "
                "PLAINTEXT in Cosmos. This is acceptable only for local dev."
            )
            _cipher = NullCipher()
            return _cipher

        try:
            _cipher = EnvelopeCipher(vault_uri, kek_name, dek_cache_ttl=ttl)
            logger.info(
                "EnvelopeCipher initialized (vault=%s, kek=%s, kek_version=%s, ttl=%ds)",
                vault_uri, kek_name, _cipher.kek_version, ttl,
            )
        except Exception as e:
            logger.exception(
                "EnvelopeCipher init failed; falling back to NullCipher. "
                "api_keys will not be encrypted at rest until this is fixed: %s",
                e,
            )
            _cipher = NullCipher()
        return _cipher


def reset_cipher_for_tests() -> None:
    """Test hook — drop the singleton so the next get_cipher() reinitializes."""
    global _cipher
    with _cipher_lock:
        _cipher = None
