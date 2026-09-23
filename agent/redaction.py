"""Remove credential-shaped fields before evidence reaches chat or audit."""
from __future__ import annotations

import re


_KEYS = re.compile(r"^(password|passwd|secret|client_?secret|api_?key|account_?key|access_?token|refresh_?token|token|authorization|connection_?string|sas_?token|private_?key)$", re.I)
_VALUES = [
    (re.compile(r"(?i)(Bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:AccountKey|SharedAccessKey|Password|ClientSecret|api_key|access_token|sig)\s*=\s*)[^;\s&\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1[REDACTED]@"),
]


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _KEYS.match(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern, replacement in _VALUES:
            value = pattern.sub(replacement, value)
    return value
