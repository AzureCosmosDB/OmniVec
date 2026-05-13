"""OmniVec Agent — system-wide AI ops agent (Phase 1: read-only diagnostics).

This package exposes a FastAPI app (``agent.api.app``) deployed alongside the
existing OmniVec services. It provides a chat interface backed by an LLM with
tool-calling, where each tool wraps a read-only diagnostic of the OmniVec
platform (REST API, Kubernetes pods, Cosmos, Service Bus, in-memory metrics).

Distinct from ``/api/assistants/*`` in ``api/api.py``: that endpoint exposes
per-user RAG bots over customer data. This package manages OmniVec *itself*.
"""

__version__ = "0.1.0"
