import importlib.util
import sys
from pathlib import Path

import pytest


MCP_PATH = Path(__file__).parents[2] / "mcp_servers" / "cosmos"
MODULE_PATH = MCP_PATH / "cosmos_tools.py"
SPEC = importlib.util.spec_from_file_location("cosmos_tools", MODULE_PATH)
cosmos_tools = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cosmos_tools)


@pytest.mark.parametrize("value, expected", [(1, 1), ("3", 3), (10, 10)])
def test_normalize_top_k_accepts_bounded_integers(value, expected):
    assert cosmos_tools.normalize_top_k(value) == expected


@pytest.mark.parametrize("value", [0, 11, "many", None, True, 3.5])
def test_normalize_top_k_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        cosmos_tools.normalize_top_k(value)


def test_vector_query_only_interpolates_validated_identifiers():
    query = cosmos_tools.vector_query(
        "4", "embedding", "id,title,body",
        {"source_id": "src-demo", "pipeline_id": "pip-demo"},
    )
    assert "SELECT TOP 4" in query
    assert "c.id, c.title, c.body" in query
    assert "@embedding" in query
    assert "VectorDistance" in query
    assert "c.source_id = @source_id" in query
    assert "c.pipeline_id = @pipeline_id" in query


def test_container_allowlist_is_enforced():
    allowed = cosmos_tools.parse_allowed_containers("documents,vectors-prod")
    assert cosmos_tools.select_container("", allowed, "documents") == "documents"
    with pytest.raises(ValueError):
        cosmos_tools.select_container("metadata", allowed, "documents")


def test_container_allowlist_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicates"):
        cosmos_tools.parse_allowed_containers("documents,documents")


@pytest.mark.parametrize(
    "vector_field, fields",
    [
        ("embedding); DELETE", "id"),
        ("embedding", "id,body FROM secrets"),
        ("x" * 256, "id"),
    ],
)
def test_vector_query_rejects_unsafe_identifiers(vector_field, fields):
    with pytest.raises(ValueError):
        cosmos_tools.vector_query(3, vector_field, fields)


def test_vector_query_rejects_unapproved_filter_fields():
    with pytest.raises(ValueError, match="unsupported filter field"):
        cosmos_tools.vector_query(
            3, "embedding", "id,content", {"tenant_id": "tenant-a"}
        )


def test_public_document_excludes_internal_fields_and_normalizes_distance():
    result = cosmos_tools.public_document(
        {
            "id": "travel",
            "title": "Travel",
            "body": "Use economy airfare.",
            "source_ref": "travel-expense",
            "distance": 0.123456789,
            "_etag": "secret-internal-value",
        }
    )
    assert result == {
        "id": "travel",
        "title": "Travel",
        "body": "Use economy airfare.",
        "source_ref": "travel-expense",
        "distance": 0.123457,
    }


def test_function_app_indexes_all_mcp_and_http_endpoints():
    sys.path.insert(0, str(MCP_PATH))
    try:
        spec = importlib.util.spec_from_file_location(
            "cosmos_mcp_function_app",
            MCP_PATH / "function_app.py",
        )
        function_app = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(function_app)
    finally:
        sys.path.remove(str(MCP_PATH))

    assert {function.get_function_name() for function in function_app.app.get_functions()} == {
        "list_allowed_containers",
        "list_documents",
        "mcp_http",
        "vector_search",
    }
