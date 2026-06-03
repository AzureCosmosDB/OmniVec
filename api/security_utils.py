"""Security utilities — outbound URL validation, SQL identifier validation,
URL-segment encoding, and temp-path containment checks.

These helpers are used to mitigate findings from CodeQL static analysis:

* py/full-ssrf, py/partial-ssrf — wrap user-controlled URLs with
  ``validate_outbound_url`` and user-controlled URL path segments with
  ``safe_url_segment`` before passing them to outbound HTTP calls.
* py/sql-injection — validate identifiers with ``validate_sql_identifier``
  before interpolating them into SQL strings (parameter binding cannot be
  used for table/column names).
* py/path-injection — guard ``os.unlink`` / ``open`` etc. with
  ``is_safe_temp_path``.

The helpers are intentionally permissive enough to keep the existing
deployment working (Azure blob hosts + an env-driven allowlist), but
strict enough to reject the canonical SSRF payloads (raw IPs,
metadata-service hostnames, private ranges, credentials in URL,
non-http schemes).
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
from typing import Iterable, Optional
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------------------
# Outbound URL allowlist
# ---------------------------------------------------------------------------

# Default suffix allowlist — Azure Storage Blob endpoints across clouds.
_DEFAULT_HOST_SUFFIXES: tuple[str, ...] = (
    ".blob.core.windows.net",
    ".blob.core.usgovcloudapi.net",
    ".blob.core.chinacloudapi.cn",
    ".blob.core.cloudapi.de",
)


def _env_suffix_list(var: str) -> tuple[str, ...]:
    raw = os.getenv(var, "") or ""
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def get_host_allowlist() -> tuple[str, ...]:
    """Allowed host suffixes for outbound HTTP calls that take a URL from
    request input. Built-in: Azure blob hosts. Extend via the
    ``OUTBOUND_HOST_ALLOWLIST`` env var (comma-separated suffixes,
    leading dot recommended)."""
    extra = _env_suffix_list("OUTBOUND_HOST_ALLOWLIST")
    return _DEFAULT_HOST_SUFFIXES + extra


def is_azure_blob_host(host: str) -> bool:
    """Strict suffix match against Azure blob host families. Replaces the
    ``".blob.core.windows.net" in url`` substring check that was flagged
    by ``py/incomplete-url-substring-sanitization``."""
    if not host:
        return False
    h = host.lower()
    return any(h == suf.lstrip(".") or h.endswith(suf) for suf in _DEFAULT_HOST_SUFFIXES)


def _host_is_private_ip(host: str) -> bool:
    """Return True when ``host`` parses as a literal IP and that IP is
    private/loopback/link-local/reserved/multicast."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_outbound_url(
    url: str,
    *,
    allow_suffixes: Optional[Iterable[str]] = None,
    allow_schemes: Iterable[str] = ("http", "https"),
) -> str:
    """Validate ``url`` for outbound fetching from a user-controlled input.

    Rejects:
      * non-http(s) schemes (file://, ftp://, gopher://, …)
      * URLs with embedded credentials (``user:pass@host``)
      * literal IPs that are private/loopback/link-local/reserved
      * hosts not matching the suffix allowlist (when one is in effect)

    Returns the original URL on success; raises ``ValueError`` on rejection.
    """
    if not url or not isinstance(url, str):
        raise ValueError("url must be a non-empty string")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in tuple(allow_schemes):
        raise ValueError(f"scheme '{scheme}' not allowed")

    if parsed.username or parsed.password:
        raise ValueError("URL must not contain credentials")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL must contain a host")

    if _host_is_private_ip(host):
        raise ValueError(f"host '{host}' resolves to a private/reserved IP")

    suffixes = tuple(allow_suffixes) if allow_suffixes is not None else get_host_allowlist()
    if suffixes:
        if not any(host == suf.lstrip(".") or host.endswith(suf) for suf in suffixes):
            raise ValueError(f"host '{host}' is not in the outbound allowlist")

    return url


# ---------------------------------------------------------------------------
# SQL identifier validation
# ---------------------------------------------------------------------------

# Allow A-Z a-z 0-9 _ and a single optional schema-qualified prefix.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def validate_sql_identifier(name: str, *, allow_dot: bool = False) -> str:
    """Return ``name`` if it is a safe SQL identifier; otherwise raise
    ``ValueError``. When ``allow_dot`` is True, ``schema.table`` is also
    accepted (each part validated separately)."""
    if not isinstance(name, str) or not name:
        raise ValueError("identifier must be a non-empty string")
    parts = name.split(".") if allow_dot else [name]
    if allow_dot and len(parts) > 2:
        raise ValueError("identifier may have at most one dot (schema.table)")
    for part in parts:
        if not _IDENT_RE.match(part):
            raise ValueError(f"invalid SQL identifier: {part!r}")
    return name


# ---------------------------------------------------------------------------
# URL path segment encoding (defends against partial-ssrf via FastAPI path params)
# ---------------------------------------------------------------------------

# Names that can appear in our path segments — IDs (mdl-*, dst-*, src-*,
# pip-*, trp-*, asst-*) plus user-defined transform/pipeline names.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

# Caller-id / agent-session segments — also allow '@' and '+' for UPNs / emails.
_AGENT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.+@\-]{1,256}$")


def safe_agent_segment(value: str) -> str:
    """Validate a caller-id or session-id for use as a URL path segment.

    Accepts the union of IDs, GUIDs, emails and UPNs. Rejects anything
    that could escape the segment (``/``, ``?``, ``#``, control chars,
    or path traversal). Returns the value unchanged on success — the
    regex guarantees it is already safe to interpolate into a URL.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("segment must be a non-empty string")
    if not _AGENT_SEGMENT_RE.match(value):
        raise ValueError(f"unsafe agent segment: {value!r}")
    if value in (".", ".."):
        raise ValueError("segment must not be '.' or '..'")
    return value


def safe_url_segment(value: str) -> str:
    """Validate and percent-encode ``value`` for use as a single URL path
    segment. Rejects any segment containing characters that could escape
    the segment boundary (``/``, ``?``, ``#``, ``..``, control chars).
    """
    if not isinstance(value, str) or not value:
        raise ValueError("segment must be a non-empty string")
    if not _SEGMENT_RE.match(value):
        raise ValueError(f"unsafe URL segment: {value!r}")
    if value in (".", ".."):
        raise ValueError("segment must not be '.' or '..'")
    # Even though _SEGMENT_RE already excludes /?#, keep quote() as
    # defence-in-depth so future regex edits cannot reintroduce a hole.
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Temp-path containment
# ---------------------------------------------------------------------------

def is_safe_temp_path(path: str) -> bool:
    """Return True if ``path``, after symlink resolution, lies inside the
    process's temporary directory. Use this to guard ``os.unlink`` calls
    on filenames that originated from a context dictionary or other
    indirect source."""
    if not path or not isinstance(path, str):
        return False
    try:
        resolved = os.path.realpath(path)
        tmp_root = os.path.realpath(tempfile.gettempdir())
        return os.path.commonpath([resolved, tmp_root]) == tmp_root
    except (ValueError, OSError):
        return False
