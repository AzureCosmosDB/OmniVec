"""OmniVec Agent FastAPI app — Phase 1 (read-only diagnostics)."""
from __future__ import annotations

import asyncio
import json
import logging
import logging as _logging
import os
import re as _re
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent_loop import run_agent, stream_events
from .audit import get_audit_writer
from .auth import CallerIdentity, require_internal_caller
from .session_store import get_session_store
from .tools import list_tools


# ---------------------------------------------------------------------------
# CR/LF/control-char log scrubber — matches docgrok/search pattern.
# ---------------------------------------------------------------------------
class _CtrlCharLogFilter(_logging.Filter):
    _CTRL_RE = _re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\r\n|\r|\n')

    @classmethod
    def _scrub(cls, v):
        return cls._CTRL_RE.sub(' ', v) if isinstance(v, str) else v

    def filter(self, record):
        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


_logging.getLogger().addFilter(_CtrlCharLogFilter())
for _h in _logging.getLogger().handlers:
    _h.addFilter(_CtrlCharLogFilter())
logger = logging.getLogger("agent")


_DEBUG = os.getenv("OMNIVEC_DEBUG", "").lower() in ("true", "1")
app = FastAPI(
    title="OmniVec Agent",
    version="0.1.0",
    description="In-cluster AI ops agent — Phase 1 (read-only diagnostics).",
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    openapi_url="/openapi.json" if _DEBUG else None,
)


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str = Field(..., description="'user' | 'assistant' | 'tool' | 'system'")
    content: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    model_id: Optional[str] = None
    session_id: Optional[str] = None


class ToolDescriptor(BaseModel):
    name: str
    description: str
    role: str
    readonly: bool
    parameters: dict


class ToolListResponse(BaseModel):
    role: str
    tools: list[ToolDescriptor]


class SessionSummary(BaseModel):
    id: str
    user_id: str
    title: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int = 0


class SessionTranscript(BaseModel):
    id: str
    user_id: str
    title: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
@app.get("/v1/health")
async def health() -> dict:
    return {"status": "healthy", "service": "omnivec-agent", "version": "0.1.0"}


@app.get("/v1/ready")
async def ready() -> dict:
    return {"status": "ready"}


@app.get("/v1/tools", response_model=ToolListResponse)
async def list_tools_endpoint(caller: CallerIdentity = Depends(require_internal_caller)) -> ToolListResponse:
    tools = list_tools(caller.role)
    return ToolListResponse(
        role=caller.role,
        tools=[
            ToolDescriptor(
                name=t.name, description=t.description, role=t.role,
                readonly=t.readonly, parameters=t.params.model_json_schema(),
            )
            for t in tools
        ],
    )


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, default=str)}\n\n".encode("utf-8")


@app.post("/v1/chat")
async def chat(req: ChatRequest, caller: CallerIdentity = Depends(require_internal_caller)) -> StreamingResponse:
    sessions = get_session_store()
    audit = get_audit_writer()

    # Resolve / create session.
    session = None
    if req.session_id:
        session = await sessions.get(caller.caller_id, req.session_id)
    if session is None:
        first = next((m.content for m in req.messages if m.role == "user"), "")
        session = await sessions.create_session(caller.caller_id, title=first[:60])

    # Extract last user message + prior history.
    user_messages = [m for m in req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="messages must contain at least one user message")
    last_user = user_messages[-1].content
    history = [m.model_dump() for m in req.messages[:-1]]

    await sessions.append_message(caller.caller_id, session["id"], {"role": "user", "content": last_user})

    queue: asyncio.Queue = asyncio.Queue()

    async def driver():
        await run_agent(
            queue=queue,
            user_message=last_user,
            history=history,
            role=caller.role,
            model_id=req.model_id,
            caller_id=caller.caller_id,
            session_id=session["id"],
            audit=audit,
        )

    task = asyncio.create_task(driver())

    async def emit():
        yield _sse({"type": "session", "session_id": session["id"]})
        try:
            async for evt in stream_events(queue):
                if evt.get("type") == "final":
                    await sessions.append_message(
                        caller.caller_id, session["id"],
                        {"role": "assistant", "content": evt.get("text", "")},
                    )
                yield _sse(evt)
        finally:
            await task
        yield _sse({"type": "done"})

    return StreamingResponse(emit(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/v1/sessions/{user}", response_model=list[SessionSummary])
async def list_sessions(user: str, caller: CallerIdentity = Depends(require_internal_caller)) -> list[SessionSummary]:
    if caller.caller_id != user and not caller.is_admin:
        raise HTTPException(status_code=403, detail="cannot list other users' sessions")
    return [SessionSummary(**s) for s in await get_session_store().list_for_user(user)]


@app.get("/v1/sessions/{user}/{session_id}", response_model=SessionTranscript)
async def get_session(user: str, session_id: str, caller: CallerIdentity = Depends(require_internal_caller)) -> SessionTranscript:
    if caller.caller_id != user and not caller.is_admin:
        raise HTTPException(status_code=403, detail="cannot read other users' sessions")
    doc = await get_session_store().get(user, session_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionTranscript(**doc)


@app.delete("/v1/sessions/{user}/{session_id}")
async def delete_session(user: str, session_id: str, caller: CallerIdentity = Depends(require_internal_caller)) -> dict:
    if caller.caller_id != user and not caller.is_admin:
        raise HTTPException(status_code=403, detail="cannot delete other users' sessions")
    ok = await get_session_store().delete(user, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": True, "session_id": session_id}


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
