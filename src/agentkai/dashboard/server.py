"""FastAPI backend for the AgentKai local dashboard.

Security posture (v1, per research/ARCHITECTURE.md §8):
  - binds 127.0.0.1 ONLY — any other host is refused, no exceptions
  - per-launch random bearer token, required on every request
    (``?token=`` query param or ``Authorization: Bearer`` header;
    EventSource can only use the query param)
  - the token is printed ONCE at startup and never logged anywhere;
    uvicorn access logging is disabled so tokens never land in logs

Integration status: the chat/run endpoints drive the REAL agent loop
(:class:`agentkai.agent.Agent`). Streaming text deltas, tool calls and
permission-gate approvals all flow over the SSE event stream; the durable
event log lives at ``~/.agentkai/runs/<run_id>/events.jsonl`` and the
dashboard store is a live projection of it. Memory and scheduler endpoints
are wired to the real ``Memory`` and ``Scheduler`` classes.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent import Agent
from ..memory import Memory
from ..permissions import Action, PermissionGate
from ..providers import ALIASES, ProviderConfig, resolve_model
from ..scheduler import Job, Scheduler, parse_at, parse_cron
from ..tools import default_registry

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "0.3.0"

DEFAULT_MODEL_ALIAS = "claude"
APPROVAL_TIMEOUT_S = 600.0  # how long a chat approval card waits for a click


# --------------------------------------------------------------------------
# In-memory run store (live projection; durable log is events.jsonl)
# --------------------------------------------------------------------------

class RunStore:
    """Holds runs + their live event projection.

    The durable source of truth is each run's
    ``~/.agentkai/runs/<run_id>/events.jsonl``; this store is the live,
    in-memory projection the SSE stream serves. All ``append`` calls from
    agent worker threads MUST go through the event loop
    (``loop.call_soon_threadsafe``) — ``asyncio.Queue.put_nowait`` is not
    thread-safe.
    """

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def create_run(self, prompt: str, model: str) -> str:
        run_id = secrets.token_hex(8)
        now = time.time()
        with self._lock:
            self._runs[run_id] = {
                "id": run_id,
                "prompt": prompt,
                "model": model,
                "status": "running",
                "created_at": now,
                "finished_at": None,
            }
            self._events[run_id] = []
            self._queues[run_id] = []
        return run_id

    def append(self, run_id: str, etype: str, data: dict[str, Any]) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": time.time(),
                "run_id": run_id,
                "type": etype,
                "data": data,
            }
            self._events[run_id].append(event)
            queues = list(self._queues.get(run_id, []))
        for q in queues:
            q.put_nowait(event)
        return event

    def finish(self, run_id: str, status: str = "done") -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]["status"] = status
                self._runs[run_id]["finished_at"] = time.time()

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        return sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_events(self, run_id: str) -> list[dict[str, Any]] | None:
        with self._lock:
            evs = self._events.get(run_id)
            return list(evs) if evs is not None else None

    def annotate(self, run_id: str, **fields: Any) -> None:
        """Attach extra fields (e.g. the agent's own run id) to a run."""
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].update(fields)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            qs = self._queues.get(run_id, [])
            if q in qs:
                qs.remove(q)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class ChatRequest(BaseModel):
    prompt: str
    model: str | None = None


class ApproveRequest(BaseModel):
    action_id: str
    approved: bool


_JOB_TYPES = ("cron", "at", "heartbeat", "dreaming")


class JobUpsertRequest(BaseModel):
    name: str
    schedule: str  # 5-field cron, or "@at:<ISO datetime>" for one-shots
    command: str = ""  # legacy shell job (mutually exclusive-ish with prompt)
    prompt: str = ""  # agent prompt for new-style jobs
    model_alias: str = "claude"
    job_type: str = "cron"
    enabled: bool = True


class MemoryWriteRequest(BaseModel):
    content: str


# Cap a single memory write: the dashboard edits markdown files, not blobs.
MAX_MEMORY_BYTES = 1_000_000


def _validate_job_payload(body: JobUpsertRequest) -> Job:
    """Turn a validated request model into a Job, raising HTTPException(400)
    on anything the scheduler cannot run. Mirrors the rules of
    ``agentkai scheduler add`` so the dashboard and the CLI accept the
    same jobs."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "job name is required")
    if len(name) > 128:
        raise HTTPException(400, "job name too long (max 128 chars)")
    if body.job_type not in _JOB_TYPES:
        raise HTTPException(
            400, f"unknown job type {body.job_type!r}; "
                 f"expected one of {', '.join(_JOB_TYPES)}")
    schedule = (body.schedule or "").strip()
    try:
        if body.job_type == "at":
            parse_at(schedule)
        else:
            parse_cron(schedule)
    except ValueError as exc:
        raise HTTPException(400, f"bad schedule: {exc}") from exc
    prompt = (body.prompt or "").strip()
    command = (body.command or "").strip()
    if not prompt and not command and body.job_type in ("cron", "at"):
        raise HTTPException(
            400, "cron/at jobs need a 'prompt' (agent job) or a "
                 "'command' (shell job)")
    return Job(name=name, schedule=schedule, command=command, prompt=prompt,
               model_alias=(body.model_alias or "claude").strip() or "claude",
               job_type=body.job_type, enabled=bool(body.enabled))


def _job_json(j) -> dict:
    return {"name": j.name, "schedule": j.schedule, "command": j.command,
            "prompt": j.prompt, "model_alias": j.model_alias,
            "job_type": j.job_type, "enabled": j.enabled,
            "last_run": j.last_run}


# --------------------------------------------------------------------------
# Agent wiring
# --------------------------------------------------------------------------

def _provider_prefix(model: str) -> str:
    """LiteLLM model string -> provider prefix, e.g. 'anthropic'."""
    return model.split("/", 1)[0].split(":", 1)[0].lower()


def _check_provider_key(model: str, config: ProviderConfig) -> str | None:
    """Return an honest error message when the model's provider has no key,
    or None when the model can run (key present or keyless provider)."""
    from ..providers import KEY_ENV
    prefix = _provider_prefix(model)
    env_var = KEY_ENV.get(prefix)
    if env_var is None:
        return None  # keyless provider (e.g. ollama) or unknown prefix
    if config.key_status().get(prefix) == "set":
        return None
    return (
        f"No API key configured for provider '{prefix}' "
        f"(model '{model}'). Set the {env_var} environment variable, or add "
        f"it under providers in ~/.agentkai/config.yaml — see docs/models.md. "
        f"The agent did not run; nothing was faked."
    )


def _default_agent_factory(model_alias: str,
                           gate: PermissionGate) -> Agent:
    """Build a real Agent for a dashboard chat run."""
    return Agent(model=model_alias, gate=gate)


async def _run_agent(app: FastAPI, store: RunStore, run_id: str,
                     prompt: str, model_alias: str) -> None:
    """Drive the real agent loop for one chat run, projecting its events
    into the store for SSE. Never fakes a reply: failures surface as
    ``error`` events and an ``error`` run status."""
    loop = asyncio.get_running_loop()
    agent_factory: Callable = app.state.agent_factory
    check_keys: bool = app.state.check_provider_keys

    def emit(etype: str, data: dict[str, Any]) -> None:
        # Called from the agent worker thread: hop onto the event loop.
        loop.call_soon_threadsafe(store.append, run_id, etype, data)

    resolved = resolve_model(model_alias)
    store.append(run_id, "run_started",
                 {"prompt": prompt, "model": resolved})

    if check_keys:
        config = ProviderConfig.load()
        key_error = _check_provider_key(resolved, config)
        if key_error:
            store.append(run_id, "error", {"message": key_error})
            store.append(run_id, "run_finished", {"status": "error"})
            store.finish(run_id, "error")
            return

    # -- permission gate: "ask" becomes an approval card in the chat UI --
    risk_by_tool: dict[str, str] = {}

    def ask_via_dashboard(action: Action) -> bool:
        action_id = uuid.uuid4().hex[:8]
        decided = threading.Event()
        box: dict[str, Any] = {"approved": False}
        risk_by_tool[action.tool] = action.risk
        with app.state.approvals_lock:
            app.state.pending_approvals[run_id] = {
                "action_id": action_id,
                "tool": action.tool,
                "args": action.args,
                "risk": action.risk,
                "event": decided,
                "box": box,
            }
        emit("approval", {"tool": action.tool, "args": action.args,
                          "risk": action.risk, "decision": "pending",
                          "action_id": action_id})
        ok = decided.wait(timeout=APPROVAL_TIMEOUT_S)
        with app.state.approvals_lock:
            app.state.pending_approvals.pop(run_id, None)
        approved = bool(ok and box["approved"])
        emit("approval", {"tool": action.tool, "args": action.args,
                          "risk": action.risk,
                          "decision": "allow" if approved else "deny",
                          "action_id": action_id})
        return approved

    def on_agent_event(ev: Any) -> None:
        """Live bridge: project the agent's own event log into dashboard
        SSE events the moment they are recorded (called in the agent's
        worker thread). Text deltas and approvals stream through their own
        paths; this covers structural tool events."""
        data = ev.data or {}
        if ev.type == "tool_call":
            emit("tool_call", {
                "tool": data.get("name"),
                "args": data.get("arguments", {}),
                "risk": risk_by_tool.get(data.get("name", ""), ""),
                "call_id": data.get("id"),
            })
        elif ev.type == "tool_result":
            emit("tool_result", {
                "tool": data.get("name"),
                "ok": data.get("ok", True),
                "output": data.get("output", ""),
            })

    deltas_seen = 0

    def on_text(delta: str) -> None:
        nonlocal deltas_seen
        deltas_seen += 1
        emit("message_delta", {"role": "assistant", "text": delta})

    try:
        gate = PermissionGate(ask_callback=ask_via_dashboard)
        agent = agent_factory(model_alias, gate)
        # Factories return a real Agent; attach the live event bridge
        # before the loop starts so tool calls stream as they execute.
        agent.on_event = on_agent_event
        result = await asyncio.to_thread(agent.run, prompt, None, on_text)
    except Exception as exc:  # noqa: BLE001 - chat must never die silently
        store.append(run_id, "error",
                     {"message": f"{type(exc).__name__}: {exc}"})
        store.finish(run_id, "error")
        return

    store.annotate(run_id, agent_run_id=result.events_path.parent.name)

    if deltas_seen == 0 and result.text.strip():
        # Provider didn't stream (or text came only from tool flow):
        # deliver the final text as one honest message event.
        store.append(run_id, "message",
                     {"role": "assistant", "text": result.text})

    if result.status == "done":
        store.append(run_id, "run_finished", {"status": "done"})
        store.finish(run_id, "done")
    else:
        msg = result.error or f"run {result.status}"
        store.append(run_id, "error", {"message": msg})
        store.append(run_id, "run_finished", {"status": "error"})
        store.finish(run_id, "error")


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(token: str | None = None,
               agent_factory: Callable[[str, PermissionGate], Agent]
               | None = None,
               scheduler: Scheduler | None = None,
               memory: Memory | None = None) -> FastAPI:
    """Build the dashboard app. A random token is generated per launch;
    pass an explicit token only in tests.

    ``agent_factory(model_alias, gate) -> Agent`` builds the agent for chat
    runs; the default builds the real agent loop. Tests inject a fake
    (which also skips the provider-key precheck).

    ``scheduler`` may be a pre-built :class:`~agentkai.scheduler.Scheduler`
    (tests inject one backed by a temp SQLite file); the default is the
    real user store at ``~/.agentkai/scheduler.db``.

    ``memory`` may be a pre-built :class:`~agentkai.memory.Memory`
    (tests inject one rooted at a temp dir); the default is the real user
    store at ``~/.agentkai/memory``.
    """
    token = token or secrets.token_urlsafe(32)
    store = RunStore()
    started_at = time.time()
    memory = memory or Memory()
    scheduler = scheduler or Scheduler()
    # The built-in tool set is static, but chat runs build per-run
    # registries: build one listing source per app launch. MCP-attached
    # remote tools are per-run and are not listed here (see API.md).
    tool_registry = default_registry()

    app = FastAPI(title="agentkai dashboard", docs_url=None, redoc_url=None,
                  openapi_url=None)

    # ---- auth middleware: every request needs the launch token ----
    # (static UI shell — css/js/img with no data — is served without a
    # token so the page can bootstrap; the whole server is still
    # localhost-only and every /api/* + SSE route stays token-gated)
    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        if _token_ok(request, token):
            return await call_next(request)
        return JSONResponse({"detail": "invalid or missing token"}, 401)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ---- pages ----
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)),
              name="static")

    # ---- status ----
    @app.get("/api/status")
    async def status():
        return {
            "ok": True,
            "version": VERSION,
            "uptime_s": round(time.time() - started_at, 1),
            "model_default": resolve_model(DEFAULT_MODEL_ALIAS),
            "runs_active": sum(1 for r in store.list_runs()
                               if r["status"] == "running"),
        }

    # ---- models ----
    @app.get("/api/models")
    async def models():
        config = ProviderConfig.load()
        aliases = {**ALIASES, **config.extra_aliases}
        return {
            "default": DEFAULT_MODEL_ALIAS,
            "models": [{"alias": a, "resolved": resolve_model(a)}
                       for a in sorted(aliases)],
            "key_status": config.key_status(),
        }

    # ---- runs ----
    @app.get("/api/runs")
    async def list_runs():
        return {"runs": store.list_runs()}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return run

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str):
        events = store.get_events(run_id)
        if events is None:
            raise HTTPException(404, "unknown run")
        return {"run_id": run_id, "events": events}

    # ---- chat: starts a REAL agent run; stream via /api/events?run_id= ----
    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if not req.prompt.strip():
            raise HTTPException(400, "prompt is empty")
        model = (req.model or DEFAULT_MODEL_ALIAS).strip() or \
            DEFAULT_MODEL_ALIAS
        run_id = store.create_run(req.prompt.strip(), model)
        asyncio.create_task(_run_agent(app, store, run_id,
                                       req.prompt.strip(), model))
        return {"run_id": run_id}

    # ---- approval: resolve a pending permission-gate prompt ----
    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, req: ApproveRequest):
        with app.state.approvals_lock:
            pending = app.state.pending_approvals.get(run_id)
        if pending is None:
            raise HTTPException(404, "no pending approval for this run")
        if pending["action_id"] != req.action_id:
            raise HTTPException(400, "action_id mismatch")
        pending["box"]["approved"] = bool(req.approved)
        pending["event"].set()
        return {"ok": True}

    # ---- SSE event stream ----
    @app.get("/api/events")
    async def events(request: Request, run_id: str | None = None):
        if run_id is not None and store.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        return StreamingResponse(
            _event_stream(request, store, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- memory: real Memory class, full read/write (backup kept) ----
    @app.get("/api/memory")
    async def memory_list():
        names = ["SOUL.md", "USER.md", "MEMORY.md",
                 "AGENTS.md", "IDENTITY.md", "TOOLS.md"]
        files = [{"name": n, "exists": bool(memory.read(n))}
                 for n in names]
        daily = sorted(
            (p.name for p in memory.root.glob("[0-9][0-9][0-9][0-9]-*.md")),
            reverse=True,
        )
        files += [{"name": n, "exists": True} for n in daily]
        return {"root": str(memory.root), "files": files}

    @app.get("/api/memory/{name}")
    async def memory_read(name: str):
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "invalid name")
        content = memory.read(name)
        if not content and not (memory.root / name).exists():
            raise HTTPException(404, "no such memory file")
        return {"name": name, "content": content}

    @app.put("/api/memory/{name}")
    async def memory_write(name: str, req: MemoryWriteRequest):
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "invalid name")
        raw = req.content.encode("utf-8")
        if len(raw) > MAX_MEMORY_BYTES:
            raise HTTPException(
                400, f"content too large ({len(raw)} bytes; "
                     f"max {MAX_MEMORY_BYTES})")
        # Disk I/O leaves the event loop via to_thread (same pattern as
        # scheduler job runs); the rename inside Memory.replace is atomic.
        created, backup = await asyncio.to_thread(memory.replace, name,
                                                  req.content)
        return JSONResponse(
            {"name": name, "created": created, "backup": backup,
             "bytes": len(raw)},
            status_code=201 if created else 200)

    # ---- tools: built-in registry listing (metadata only) ----
    @app.get("/api/tools")
    async def tool_registry_list():
        return {"tools": tool_registry.describe()}

    # ---- scheduler: real Scheduler class, full job management ----
    @app.get("/api/scheduler/jobs")
    async def scheduler_jobs():
        jobs = scheduler.list()
        return {"db": str(scheduler.db_path),
                "jobs": [_job_json(j) for j in jobs]}

    @app.post("/api/scheduler/jobs", status_code=201)
    async def scheduler_job_create(body: JobUpsertRequest):
        job = _validate_job_payload(body)
        created = scheduler.get(job.name) is None
        scheduler.add(job)
        return JSONResponse({"ok": True, "name": job.name,
                             "created": created, "job": _job_json(job)},
                            status_code=201 if created else 200)

    @app.delete("/api/scheduler/jobs/{name}")
    async def scheduler_job_delete(name: str):
        if not scheduler.remove(name):
            raise HTTPException(404, f"no job named {name!r}")
        return {"ok": True, "name": name}

    @app.post("/api/scheduler/jobs/{name}/run")
    async def scheduler_job_run(name: str):
        job = scheduler.get(name)
        if job is None:
            raise HTTPException(404, f"no job named {name!r}")
        if not job.enabled:
            raise HTTPException(400, f"job {name!r} is disabled; "
                                     "re-enable it before running")
        # Same pattern as chat runs: blocking agent work leaves the event
        # loop via to_thread. run_job() records the outcome in job_runs.
        result = await asyncio.to_thread(scheduler.run_job, name)
        return {"name": name, "status": result["status"],
                "summary": result.get("summary", ""),
                "started_ts": result.get("started_ts", ""),
                "finished_ts": result.get("finished_ts", "")}

    app.state.dashboard_token = token
    app.state.store = store
    app.state.agent_factory = agent_factory or _default_agent_factory
    app.state.check_provider_keys = agent_factory is None
    app.state.pending_approvals = {}
    app.state.approvals_lock = threading.Lock()
    return app


def _token_ok(request: Request, token: str) -> bool:
    q = request.query_params.get("token", "")
    if q and hmac.compare_digest(q, token):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return hmac.compare_digest(auth[7:].strip(), token)
    return False


async def _event_stream(request: Request, store: RunStore,
                        run_id: str | None):
    """SSE: replay stored events, then stream new ones live."""
    def fmt(ev: dict) -> str:
        return f"data: {json.dumps(ev)}\n\n"

    if run_id:
        # Subscribe BEFORE replaying: each `yield` below lets the event
        # loop run the agent task, so events appended between replay and
        # subscribe would otherwise be lost (and a lost run_finished
        # hangs the stream forever). Dedupe by seq.
        q = store.subscribe(run_id)
        try:
            seen_seqs: set[int] = set()
            already_finished = False
            for ev in store.get_events(run_id) or []:
                seen_seqs.add(ev["seq"])
                if ev["type"] == "run_finished":
                    already_finished = True
                yield fmt(ev)
            if already_finished:
                return  # run ended before we subscribed; nothing live to wait for
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev["seq"] in seen_seqs:
                    continue
                seen_seqs.add(ev["seq"])
                yield fmt(ev)
                if ev["type"] == "run_finished":
                    break
        finally:
            store.unsubscribe(run_id, q)
    else:
        # activity feed: every event from every run, live only
        yield ": connected\n\n"
        last_seen: dict[str, int] = {}
        while True:
            if await request.is_disconnected():
                break
            for r in store.list_runs():
                rid = r["id"]
                evs = store.get_events(rid) or []
                seen = last_seen.get(rid, 0)
                for ev in evs:
                    if ev["seq"] > seen:
                        yield fmt(ev)
                if evs:
                    last_seen[rid] = evs[-1]["seq"]
            await asyncio.sleep(0.5)
