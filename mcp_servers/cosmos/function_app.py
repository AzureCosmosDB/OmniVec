"""Reusable, read-only MCP tools for Azure Cosmos DB."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import azure.functions as func
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from cosmos_tools import (
    list_query,
    normalize_top_k,
    parse_allowed_containers,
    public_document,
    select_container,
    vector_query,
)


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

_MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
_MCP_TOOLS = [
    {
        "name": "list_allowed_containers",
        "description": "List the Cosmos containers this MCP deployment is authorized to expose.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_documents",
        "description": "List projected documents from an allowed Cosmos DB container.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                "fields": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "vector_search",
        "description": "Embed a question and perform vector search over an allowed Cosmos container.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "container": {"type": "string"},
                "vector_field": {"type": "string"},
                "fields": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@lru_cache(maxsize=1)
def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@lru_cache(maxsize=1)
def _database():
    client = CosmosClient(_required_env("COSMOS_ENDPOINT"), credential=_credential())
    return client.get_database_client(_required_env("COSMOS_DATABASE"))


@lru_cache(maxsize=1)
def _allowed_containers() -> tuple[str, ...]:
    return parse_allowed_containers(_required_env("COSMOS_ALLOWED_CONTAINERS"))


def _container(requested: str | None):
    selected = select_container(
        requested,
        _allowed_containers(),
        os.getenv("COSMOS_DEFAULT_CONTAINER"),
    )
    return selected, _database().get_container_client(selected)


@lru_cache(maxsize=1)
def _embedding_client() -> AzureOpenAI:
    token_provider = get_bearer_token_provider(
        _credential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=_required_env("AZURE_OPENAI_ENDPOINT"),
        azure_ad_token_provider=token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01"),
    )


def _list_allowed_containers() -> str:
    return json.dumps(
        {
            "database": _required_env("COSMOS_DATABASE"),
            "containers": list(_allowed_containers()),
            "default_container": os.getenv("COSMOS_DEFAULT_CONTAINER") or None,
        }
    )


@app.mcp_tool()
def list_allowed_containers() -> str:
    """List the Cosmos containers this MCP deployment is authorized to expose."""
    return _list_allowed_containers()


def _list_documents(
    container: str = "",
    top_k: int = 10,
    fields: str = "id,title,body,source_ref",
) -> str:
    limit = normalize_top_k(top_k)
    selected, client = _container(container)
    try:
        rows = client.query_items(
            query=list_query(limit, fields),
            enable_cross_partition_query=True,
        )
        documents = [public_document(row) for row in rows]
        return json.dumps(
            {"container": selected, "count": len(documents), "documents": documents}
        )
    except ValueError:
        raise
    except Exception:
        logging.exception("Failed to list Cosmos documents")
        raise RuntimeError("Unable to list Cosmos documents") from None


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="container",
    description="Allowed Cosmos container name. Optional when a default is configured.",
    is_required=False,
)
@app.mcp_tool_property(
    arg_name="top_k",
    description="Number of documents to return, from 1 through 10.",
    is_required=False,
)
@app.mcp_tool_property(
    arg_name="fields",
    description="Comma-separated top-level document fields to return.",
    is_required=False,
)
def list_documents(
    container: str = "",
    top_k: int = 10,
    fields: str = "id,title,body,source_ref",
) -> str:
    """List projected documents from an allowed Cosmos DB container."""
    return _list_documents(container, top_k, fields)


def _vector_search(
    question: str,
    container: str = "",
    vector_field: str = "embedding",
    fields: str = "id,title,body,source_ref",
    top_k: int = 3,
) -> str:
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be blank")
    if len(question) > 8192:
        raise ValueError("question must not exceed 8192 characters")
    limit = normalize_top_k(top_k)
    selected, client = _container(container)
    try:
        response = _embedding_client().embeddings.create(
            model=_required_env("AZURE_OPENAI_DEPLOYMENT"),
            input=question,
        )
        embedding = response.data[0].embedding
        rows = client.query_items(
            query=vector_query(limit, vector_field, fields),
            parameters=[{"name": "@embedding", "value": embedding}],
            enable_cross_partition_query=True,
        )
        matches = [public_document(row) for row in rows]
        return json.dumps(
            {
                "container": selected,
                "question": question,
                "count": len(matches),
                "matches": matches,
            }
        )
    except ValueError:
        raise
    except Exception:
        logging.exception("Failed to vector-search Cosmos documents")
        raise RuntimeError("Unable to vector-search Cosmos documents") from None


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="question",
    description="Natural-language question or search topic.",
    is_required=True,
)
@app.mcp_tool_property(
    arg_name="container",
    description="Allowed Cosmos container name. Optional when a default is configured.",
    is_required=False,
)
@app.mcp_tool_property(
    arg_name="vector_field",
    description="Top-level field containing the vector embedding.",
    is_required=False,
)
@app.mcp_tool_property(
    arg_name="fields",
    description="Comma-separated top-level fields to return with each match.",
    is_required=False,
)
@app.mcp_tool_property(
    arg_name="top_k",
    description="Number of nearest documents to return, from 1 through 10.",
    is_required=False,
)
def vector_search(
    question: str,
    container: str = "",
    vector_field: str = "embedding",
    fields: str = "id,title,body,source_ref",
    top_k: int = 3,
) -> str:
    """Embed a question and perform vector search over an allowed Cosmos container."""
    return _vector_search(question, container, vector_field, fields, top_k)


def _mcp_result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _call_tool(name: str, arguments: dict) -> dict:
    tools = {
        "list_allowed_containers": _list_allowed_containers,
        "list_documents": _list_documents,
        "vector_search": _vector_search,
    }
    tool = tools.get(name)
    if tool is None:
        raise KeyError(name)
    try:
        text = tool(**arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except (TypeError, ValueError) as error:
        return {
            "content": [{"type": "text", "text": f"Invalid tool arguments: {error}"}],
            "isError": True,
        }
    except RuntimeError as error:
        return {
            "content": [{"type": "text", "text": str(error)}],
            "isError": True,
        }


def _handle_mcp_message(message: object) -> tuple[dict | None, int]:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _mcp_error(None, -32600, "Invalid Request"), 400

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _mcp_error(request_id, -32600, "Invalid Request"), 400

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol_version = (
            requested if requested in _MCP_PROTOCOL_VERSIONS else "2025-03-26"
        )
        return (
            _mcp_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "omnivec-cosmos",
                        "version": "1.0.0",
                        "deployment_id": os.getenv("OMNIVEC_DEPLOYMENT_ID", ""),
                    },
                },
            ),
            200,
        )

    if method == "ping":
        return _mcp_result(request_id, {}), 200

    if method == "tools/list":
        return _mcp_result(request_id, {"tools": _MCP_TOOLS}), 200

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _mcp_error(request_id, -32602, "Invalid params"), 400
        try:
            result = _call_tool(name, arguments)
        except KeyError:
            return _mcp_error(request_id, -32602, f"Unknown tool: {name}"), 400
        return _mcp_result(request_id, result), 200

    if request_id is None:
        return None, 202
    return _mcp_error(request_id, -32601, "Method not found"), 404


@app.route(route="mcp", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def mcp_http(req: func.HttpRequest) -> func.HttpResponse:
    """Serve the MCP Streamable HTTP JSON-RPC surface without preview middleware."""
    try:
        message = req.get_json()
    except ValueError:
        payload, status = _mcp_error(None, -32700, "Parse error"), 400
    else:
        payload, status = _handle_mcp_message(message)

    if payload is None:
        return func.HttpResponse(status_code=status)
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status,
        mimetype="application/json",
    )
