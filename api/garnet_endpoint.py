"""Canonical Garnet endpoint identity shared by validation and ownership."""
from urllib.parse import urlsplit


def garnet_endpoint_identity(config):
    endpoint = str(config.get("endpoint") or "").strip()
    parsed = urlsplit(endpoint if "://" in endpoint else f"redis://{endpoint}")
    if (not parsed.hostname or parsed.scheme not in ("redis", "rediss")
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Garnet endpoint must identify a host without credentials or path")
    tls = config.get("tls", True)
    if not isinstance(tls, bool):
        raise ValueError("Garnet tls must be a boolean")
    port = parsed.port if parsed.port is not None else (6380 if tls else 6379)
    if not 1 <= port <= 65535:
        raise ValueError("Garnet endpoint port must be between 1 and 65535")
    return parsed.hostname.rstrip(".").lower(), port, tls
