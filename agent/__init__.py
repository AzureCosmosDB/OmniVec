"""OmniVec Agent — in-cluster diagnosis and approved, verified recovery.

This package exposes a FastAPI app (``agent.api.app``) deployed alongside the
existing OmniVec services. It provides a chat interface backed by an LLM with
tool-calling, bounded deterministic diagnostics and approval-gated operational
tools. Mutations are followed by runtime-enforced processing verification.

Distinct from ``/api/assistants/*`` in ``api/api.py``: that endpoint exposes
per-user RAG bots over customer data. This package manages OmniVec *itself*.
"""

__version__ = "0.1.0"
