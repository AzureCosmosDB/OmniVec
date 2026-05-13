"""Tool registry for the OmniVec Agent.

A *tool* is a small, JSON-schema'd async callable that the LLM can invoke
during a chat turn. Tools are registered with the ``@tool`` decorator and
discovered via ``list_tools()`` / ``get_tool()``.
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Type

from pydantic import BaseModel


@dataclass
class Tool:
    name: str
    description: str
    params: Type[BaseModel]
    callable: Callable[..., Awaitable[Any]]
    role: str = "reader"
    readonly: bool = True

    def json_schema(self) -> dict:
        """OpenAI-style function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params.model_json_schema(),
            },
        }


_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: Type[BaseModel], *, role: str = "reader", readonly: bool = True):
    """Decorator that registers an async callable as an agent tool."""
    if role not in ("reader", "admin"):
        raise ValueError(f"tool {name!r}: role must be 'reader' or 'admin'")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool {name!r}: callable must be async")
        _REGISTRY[name] = Tool(
            name=name, description=description, params=params,
            callable=fn, role=role, readonly=readonly,
        )

        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            return await fn(*args, **kwargs)
        return wrapped
    return decorator


def list_tools(role: str = "admin") -> list[Tool]:
    """Return tools visible to the given role.

    Phase 1: ``reader`` and ``admin`` see the same list (all tools are
    read-only). The role plumbing exists so Phase 2 can add mutating tools
    that only ``admin`` may invoke.
    """
    if role == "admin":
        return sorted(_REGISTRY.values(), key=lambda t: t.name)
    return sorted([t for t in _REGISTRY.values() if t.role == "reader"], key=lambda t: t.name)


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def validate_args(tool_name: str, raw: dict) -> BaseModel:
    """Validate ``raw`` against the tool's params model. Raises ValidationError."""
    t = get_tool(tool_name)
    if t is None:
        raise KeyError(f"unknown tool: {tool_name}")
    return t.params.model_validate(raw)


def _reset_registry_for_tests() -> None:
    _REGISTRY.clear()


def _load_builtin_tools() -> None:
    """Eager-import the built-in tool modules so they self-register on import."""
    from . import omnivec_api, k8s, cosmos, servicebus, metrics, mutations  # noqa: F401


_load_builtin_tools()
