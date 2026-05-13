"""Shared fixtures for the OmniVec unit/regression-trap test suite.

Each FastAPI app lives in its own sibling directory (``api/``, ``search/``,
``docgrok/``) and imports its siblings as **top-level** modules. To import
them cleanly from tests we need to:

  1. Prepend the service directory to ``sys.path``.
  2. Set the small set of env vars they read at module-import time (most
     have defaults; only a couple are required to avoid hard failures).
  3. Monkey-patch any Azure clients that are constructed at import time
     so we never touch the network.

The three apps in this repo are well-behaved: ``init_store()`` is invoked
from a FastAPI startup handler (not at import), and all env-var lookups
at module top-level have safe defaults. So the fixtures below are mostly
defensive: they pin a deterministic, network-free environment.
"""
from __future__ import annotations

import os
import sys
import pathlib
import importlib

import pytest
from hypothesis import HealthCheck, settings


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "api"
SEARCH_DIR = REPO_ROOT / "search"
DOCGROK_DIR = REPO_ROOT / "docgrok"
SCRIPTS_DIR = REPO_ROOT / "scripts"
AGENT_PARENT = REPO_ROOT  # agent is a package, imported as ``agent.*``


# ---------------------------------------------------------------------------
# Hypothesis profiles — fast (default) and thorough (HYPOTHESIS_THOROUGH=1).
# ---------------------------------------------------------------------------
settings.register_profile(
    "fast",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "thorough",
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.load_profile("thorough" if os.environ.get("HYPOTHESIS_THOROUGH") == "1" else "fast")


# ---------------------------------------------------------------------------
# Path / env helpers.
# ---------------------------------------------------------------------------
def _ensure_path(p: pathlib.Path) -> None:
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _purge_modules(names: list[str]) -> None:
    """Drop fully-qualified module names so a sibling app's import doesn't
    accidentally satisfy ``import models``/``import auth`` etc. from a
    previous fixture."""
    for n in names:
        sys.modules.pop(n, None)


def _set_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, network-free env for any of the three apps."""
    defaults = {
        "OMNIVEC_DEBUG": "1",  # turn on /docs + /openapi.json
        "OMNIVEC_ADMIN_TOKEN": "test-admin-token",
        "OMNIVEC_AAD_TENANT_ID": "",  # disable AAD path
        "OMNIVEC_AAD_AUDIENCE": "",
        "COSMOS_ENDPOINT": "https://test.example.invalid/",
        "COSMOS_DATABASE": "omnivec",
        "DOCGROK_URL": "http://docgrok.test.invalid",
        "PIPELINE_WORKER_URL": "http://pipeline-worker.test.invalid",
        "SEARCH_SERVICE_URL": "http://search.test.invalid",
        "SEARCH_INTERNAL_TOKEN": "test-internal",
        "OMNIVEC_SEARCH_TOKEN": "test-search",
        "CORS_ORIGINS": "",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


def _stub_azure_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the Azure client factories so even if some module *does* try to
    instantiate one at import time we never reach the network."""
    try:
        from azure.cosmos import CosmosClient
        monkeypatch.setattr(CosmosClient, "__init__", lambda self, *a, **kw: None, raising=False)
    except Exception:  # pragma: no cover — azure-cosmos absent
        pass
    try:
        from azure.identity import DefaultAzureCredential
        monkeypatch.setattr(DefaultAzureCredential, "__init__", lambda self, *a, **kw: None, raising=False)
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# App import fixtures. Each one isolates sys.path + module state so the
# three apps can be imported in the same pytest process without colliding.
# ---------------------------------------------------------------------------
_SIBLING_MODULE_NAMES = [
    # api/ siblings
    "api", "models", "store", "telemetry", "security_utils", "cosmos_retry",
    # search/ siblings
    "main", "auth", "schemas", "searcher",
    # docgrok/ siblings
    "admin", "pipelines", "model_store",
]


@pytest.fixture
def api_app(monkeypatch):
    """Import ``api/api.py`` and return the FastAPI app instance."""
    _set_defaults(monkeypatch)
    _stub_azure_clients(monkeypatch)
    _purge_modules(_SIBLING_MODULE_NAMES)
    monkeypatch.syspath_prepend(str(API_DIR))
    mod = importlib.import_module("api")
    # Prevent Cosmos init even if a test triggers startup.
    monkeypatch.setattr(mod, "init_store", lambda: None, raising=False)
    yield mod.app


@pytest.fixture
def search_app(monkeypatch):
    """Import ``search/main.py`` and return the FastAPI app instance."""
    _set_defaults(monkeypatch)
    _stub_azure_clients(monkeypatch)
    _purge_modules(_SIBLING_MODULE_NAMES)
    monkeypatch.syspath_prepend(str(SEARCH_DIR))
    mod = importlib.import_module("main")
    yield mod.app


@pytest.fixture
def docgrok_app(monkeypatch):
    """Import ``docgrok/api.py`` and return the FastAPI app instance."""
    _set_defaults(monkeypatch)
    _stub_azure_clients(monkeypatch)
    _purge_modules(_SIBLING_MODULE_NAMES)
    monkeypatch.syspath_prepend(str(DOCGROK_DIR))
    mod = importlib.import_module("api")
    yield mod.app


# ---------------------------------------------------------------------------
# Filter fixtures — return the `_SensitiveFilter` / `_CtrlCharLogFilter`
# *classes* from each service so property tests can exercise them without
# spinning up the full FastAPI app.
# ---------------------------------------------------------------------------
@pytest.fixture
def api_sensitive_filter(api_app):
    import sys as _s
    return _s.modules["api"]._SensitiveFilter


@pytest.fixture
def search_ctrl_filter(search_app):
    import sys as _s
    return _s.modules["main"]._CtrlCharLogFilter


@pytest.fixture
def docgrok_ctrl_filter(docgrok_app):
    import sys as _s
    return _s.modules["api"]._CtrlCharLogFilter


# ---------------------------------------------------------------------------
# Threat-model generator module loader.
# ---------------------------------------------------------------------------
@pytest.fixture
def tm7_generator(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    _purge_modules(["gen_threat_model_tm7"])
    mod = importlib.import_module("gen_threat_model_tm7")
    return mod


_AGENT_MODULES = [
    "agent", "agent.api", "agent.agent_loop", "agent.audit", "agent.auth",
    "agent.llm", "agent.session_store",
    "agent.tools", "agent.tools.omnivec_api", "agent.tools.k8s",
    "agent.tools.cosmos", "agent.tools.servicebus", "agent.tools.metrics",
]


@pytest.fixture
def agent_app(monkeypatch):
    """Import ``agent.api`` and return the FastAPI app with deterministic env."""
    _set_defaults(monkeypatch)
    _stub_azure_clients(monkeypatch)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")
    monkeypatch.setenv("OMNIVEC_NAMESPACE", "omnivec")
    monkeypatch.setenv("AOAI_ENDPOINT", "")
    monkeypatch.syspath_prepend(str(AGENT_PARENT))
    for m in _AGENT_MODULES:
        sys.modules.pop(m, None)
    mod = importlib.import_module("agent.api")
    from agent.session_store import reset_session_store_for_tests
    from agent.audit import reset_audit_writer_for_tests
    reset_session_store_for_tests()
    reset_audit_writer_for_tests()
    yield mod.app


@pytest.fixture
def api_models(monkeypatch):
    """Just the Pydantic models module from ``api/models.py``."""
    _set_defaults(monkeypatch)
    _purge_modules(_SIBLING_MODULE_NAMES)
    monkeypatch.syspath_prepend(str(API_DIR))
    return importlib.import_module("models")
