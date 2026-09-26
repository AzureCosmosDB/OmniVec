"""Pure validation and query helpers for the generic Cosmos MCP server."""

from __future__ import annotations

import re
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
_FILTER_FIELDS = ("source_id", "pipeline_id", "source_ref")


def normalize_top_k(value: Any) -> int:
    """Return a bounded result count suitable for interpolation into Cosmos SQL."""
    if isinstance(value, bool):
        raise ValueError("top_k must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value)
    else:
        raise ValueError("top_k must be an integer")
    if parsed < 1 or parsed > 10:
        raise ValueError("top_k must be between 1 and 10")
    return parsed


def parse_allowed_containers(value: str) -> tuple[str, ...]:
    """Parse and validate a comma-separated container allowlist."""
    containers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not containers:
        raise ValueError("COSMOS_ALLOWED_CONTAINERS must contain at least one container")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", item) for item in containers):
        raise ValueError("COSMOS_ALLOWED_CONTAINERS contains an invalid container name")
    if len(set(containers)) != len(containers):
        raise ValueError("COSMOS_ALLOWED_CONTAINERS must not contain duplicates")
    return containers


def select_container(
    requested: str | None,
    allowed: tuple[str, ...],
    default: str | None = None,
) -> str:
    """Resolve a requested container while enforcing the configured allowlist."""
    selected = (requested or default or "").strip()
    if not selected:
        if len(allowed) == 1:
            selected = allowed[0]
        else:
            raise ValueError("container is required when multiple containers are allowed")
    if selected not in allowed:
        raise ValueError(f"container '{selected}' is not allowed")
    return selected


def parse_fields(value: str | None) -> tuple[str, ...]:
    """Validate a comma-separated list of top-level Cosmos document fields."""
    fields = tuple(item.strip() for item in (value or "id").split(",") if item.strip())
    if not fields or len(fields) > 20:
        raise ValueError("fields must contain between 1 and 20 names")
    if any(not _IDENTIFIER.fullmatch(item) for item in fields):
        raise ValueError("fields must be comma-separated top-level property names")
    return fields


def _projection(fields: tuple[str, ...]) -> str:
    return ", ".join(f"c.{field}" for field in fields)


def list_query(top_k: Any, fields: str | None) -> str:
    """Build a bounded read-only document-list query."""
    limit = normalize_top_k(top_k)
    selected_fields = parse_fields(fields)
    return f"SELECT TOP {limit} {_projection(selected_fields)} FROM c"


def normalize_filters(filters: dict[str, Any] | None) -> dict[str, str]:
    """Validate the fixed, parameterized metadata filters exposed by the MCP tool."""
    normalized: dict[str, str] = {}
    for key, value in (filters or {}).items():
        if key not in _FILTER_FIELDS:
            raise ValueError(f"unsupported filter field: {key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-blank string")
        if len(value) > 2048:
            raise ValueError(f"{key} must not exceed 2048 characters")
        normalized[key] = value.strip()
    return normalized


def vector_query(
    top_k: Any,
    vector_field: str,
    fields: str | None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Build a vector query after validating every interpolated identifier."""
    limit = normalize_top_k(top_k)
    if not _IDENTIFIER.fullmatch(vector_field or ""):
        raise ValueError("vector_field must be a top-level property name")
    selected_fields = parse_fields(fields)
    normalized_filters = normalize_filters(filters)
    predicates = [f"IS_DEFINED(c.{vector_field})"]
    predicates.extend(f"c.{field} = @{field}" for field in normalized_filters)
    return (
        f"SELECT TOP {limit} {_projection(selected_fields)}, "
        f"VectorDistance(c.{vector_field}, @embedding) AS distance "
        f"FROM c WHERE {' AND '.join(predicates)} "
        f"ORDER BY VectorDistance(c.{vector_field}, @embedding)"
    )


def public_document(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a projected document result without exposing Cosmos internals."""
    result = {key: value for key, value in row.items() if not key.startswith("_")}
    if row.get("distance") is not None:
        result["distance"] = round(float(row["distance"]), 6)
    return result
