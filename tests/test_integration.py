"""End-to-end integration tests: the real agent loop wired through the
dashboard, the messaging gateway, the scheduler, and subagents.

Every test uses scripted fake LLM clients — no network, no API keys — but
the Agent, permission gate, event log, and SSE machinery are all real.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from agentkai.agent import Agent
from agentkai.channels.core import Channel, Gateway, InboundMessage, SessionStore
from agentkai.dashboard.server import create_app
from agentkai.permissions import PermissionGate
from agentkai.providers import LLMMessage, ToolCall
from agentkai.scheduler import Job, Scheduler
from agentkai.subagents import SubagentManager
from agentkai.tools import Registry, Tool

TOKEN = "integration-token-xyz"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeClient:
    """Scripted stand-in for LLMClient. Thread-safe; streams text in chunks."""

    def __init__(self, script: list[LLMMessage]):
        self.script = script
        self.model = "fake/test-model"
        self._i = 0
        self._lock = threading.Lock()

    def generate(self, messages, tools=None, on_delta=None, timeout=None,
                 cancel_event=None, **kwargs) -> LLMMessage:
        with self._lock:
            msg = self.script[min(self._i, len(self.script) - 1)]
            self._i += 1
        if on_delta and msg.text:
            for j in range(0, len(msg.text), 6):
                if cancel_event is not None and cancel_event.is_set():
                    break
                on_delta(msg.text[j:j + 6])
        return msg


def echo_registry(risk: str = "low") -> Registry:
    reg = Registry()
    reg.register(Tool(
        name="echo",
        description="Echo text back.",
        json_schema={"type": "object",
                     "properties": {"text": {"type": "string"}}},
        risk=risk,
        func=lambda text="": f"ECHO:{text}",
    ))
    return reg


def dashboard_factory(tmp_path, script, risk="low"):
    """agent_factory for the dashboard: real Agent, fake provider."""
    def factory(model_alias, gate):
        return Agent(model="fake", client=FakeClient(script), gate=gate,
                     registry=echo_registry(risk),
                     events_root=str(tmp_path / "runs"))
    return factory


def client_for(factory):
    app = create_app(TOKEN, agent_factory=factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport,
                             base_url="http://127.0.0.1"), app


async def collect_sse(c: httpx.AsyncClient, run_id: str,
                      timeout: float = 30) -> list[dict]:
    """Read a run's SSE stream until run_finished; return all events."""
    seen = []
    async with c.stream("GET",
                        f"/api/events?token={TOKEN}&run_id={run_id}",
                        timeout=timeout) as s:
        assert s.status_code == 200
        async for line in s.aiter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                seen.append(ev)
                if ev["type"] == "run_finished":
                    break
    return seen


# --------------------------------------------------------------------------
# (a) dashboard chat through the REAL agent loop + a real tool call
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_chat_real_agent_with_tool(tmp_path):
    script = [
        LLMMessage(text="", tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"text": "hello"})]),
        LLMMessage(text="The tool answered.", tool_calls=[]),
    ]
    c, app = client_for(dashboard_factory(tmp_path, script))
    async with c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "say hi via the tool"})
                  ).json()["run_id"]
        seen = await collect_sse(c, run_id)
        types = [e["type"] for e in seen]

        assert "run_started" in types
        assert "tool_call" in types and "tool_result" in types
        assert "run_finished" in types

        tool_call = next(e for e in seen if e["type"] == "tool_call")
        assert tool_call["data"]["tool"] == "echo"
        assert tool_call["data"]["args"] == {"text": "hello"}

        tool_result = next(e for e in seen if e["type"] == "tool_result")
        assert tool_result["data"]["ok"] is True
        assert "ECHO:hello" in tool_result["data"]["output"]

        # streamed text arrives as real deltas that concatenate correctly
        deltas = "".join(e["data"]["text"] for e in seen
                         if e["type"] == "message_delta")
        assert "The tool answered." in deltas

        # run record is done, and the durable agent log exists
        run = (await c.get(f"/api/runs/{run_id}?token={TOKEN}")).json()
        assert run["status"] == "done"
        assert run["agent_run_id"]
        log_path = (tmp_path / "runs" / run["agent_run_id"] / "events.jsonl")
        assert log_path.exists()
        logged_types = [json.loads(l)["type"]
                        for l in log_path.read_text().splitlines() if l.strip()]
        assert "run_start" in logged_types and "run_end" in logged_types
        assert "tool_call" in logged_types


# --------------------------------------------------------------------------
# liveness: tool_call must stream while the tool is still executing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_tool_events_stream_live(tmp_path):
    """A tool that blocks until released proves the dashboard's tool_call
    event is streamed live — it must arrive over SSE while the tool is
    still running, not replayed after the run completes.

    NOTE: this test runs a real uvicorn server on localhost instead of
    httpx's ASGITransport, because ASGITransport buffers the entire
    response body and cannot deliver SSE chunks incrementally.
    """
    import uvicorn

    release = threading.Event()

    def slow_tool(text: str = "") -> str:
        assert release.wait(timeout=20), "test never released the tool"
        return f"SLOW:{text}"

    def factory(model_alias, gate):
        reg = Registry()
        reg.register(Tool(
            name="slow", description="blocking tool",
            json_schema={"type": "object",
                         "properties": {"text": {"type": "string"}}},
            risk="low", func=slow_tool))
        return Agent(
            model="fake",
            client=FakeClient([
                LLMMessage(text="", tool_calls=[
                    ToolCall(id="c9", name="slow",
                             arguments={"text": "x"})]),
                LLMMessage(text="all done", tool_calls=[]),
            ]),
            gate=gate, registry=reg,
            events_root=str(tmp_path / "runs"))

    app = create_app(TOKEN, agent_factory=factory)
    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        # trust_env=False: bypass the environment's (broken) proxy vars —
        # this is a localhost test server.
        async with httpx.AsyncClient(base_url=base,
                                     trust_env=False) as c:
            run_id = (await c.post(f"/api/chat?token={TOKEN}",
                                   json={"prompt": "run the slow tool"})
                      ).json()["run_id"]
            seen: list[str] = []
            live_tool_call = False
            async with c.stream(
                    "GET", f"/api/events?token={TOKEN}&run_id={run_id}",
                    timeout=30) as s:
                assert s.status_code == 200
                async for line in s.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    seen.append(ev["type"])
                    if ev["type"] == "tool_call" and not release.is_set():
                        # The tool only returns after `release` is set, so
                        # seeing tool_call first proves it streamed live.
                        live_tool_call = True
                        release.set()
                    if ev["type"] == "run_finished":
                        break
            assert live_tool_call, \
                f"tool_call never arrived live; saw {seen}"
            assert seen.index("tool_call") < seen.index("tool_result")
            assert seen[-1] == "run_finished"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --------------------------------------------------------------------------
# approval flow: high-risk tool pauses for a dashboard approval card
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_approval_card_roundtrip(tmp_path):
    script = [
        LLMMessage(text="", tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"text": "risky"})]),
        LLMMessage(text="approved and done", tool_calls=[]),
    ]
    c, app = client_for(dashboard_factory(tmp_path, script, risk="high"))
    async with c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "do the risky thing"})
                  ).json()["run_id"]

        # wait for the pending approval event via the replay endpoint
        action_id = None
        deadline = time.time() + 20
        while time.time() < deadline and action_id is None:
            evs = (await c.get(
                f"/api/runs/{run_id}/events?token={TOKEN}")).json()["events"]
            pend = [e for e in evs if e["type"] == "approval"
                    and e["data"].get("decision") == "pending"]
            if pend:
                action_id = pend[0]["data"]["action_id"]
                assert pend[0]["data"]["tool"] == "echo"
                assert pend[0]["data"]["risk"] == "high"
            else:
                await asyncio.sleep(0.2)
        assert action_id, "no pending approval surfaced"

        # bogus action_id is rejected
        r = await c.post(f"/api/runs/{run_id}/approve?token={TOKEN}",
                         json={"action_id": "nope", "approved": True})
        assert r.status_code == 400

        # approve for real
        r = await c.post(f"/api/runs/{run_id}/approve?token={TOKEN}",
                         json={"action_id": action_id, "approved": True})
        assert r.json() == {"ok": True}

        seen = await collect_sse(c, run_id)
        types = [e["type"] for e in seen]
        assert "run_finished" in types
        run = (await c.get(f"/api/runs/{run_id}?token={TOKEN}")).json()
        assert run["status"] == "done"
        tool_result = next(e for e in seen if e["type"] == "tool_result")
        assert "ECHO:risky" in tool_result["data"]["output"]


@pytest.mark.asyncio
async def test_dashboard_denied_approval_blocks_tool(tmp_path):
    script = [
        LLMMessage(text="", tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"text": "nope"})]),
        LLMMessage(text="finished without the tool", tool_calls=[]),
    ]
    c, app = client_for(dashboard_factory(tmp_path, script, risk="high"))
    async with c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "try the risky thing"})
                  ).json()["run_id"]
        action_id = None
        deadline = time.time() + 20
        while time.time() < deadline and action_id is None:
            evs = (await c.get(
                f"/api/runs/{run_id}/events?token={TOKEN}")).json()["events"]
            pend = [e for e in evs if e["type"] == "approval"
                    and e["data"].get("decision") == "pending"]
            if pend:
                action_id = pend[0]["data"]["action_id"]
            else:
                await asyncio.sleep(0.2)
        assert action_id
        r = await c.post(f"/api/runs/{run_id}/approve?token={TOKEN}",
                         json={"action_id": action_id, "approved": False})
        assert r.json() == {"ok": True}
        seen = await collect_sse(c, run_id)
        # the tool was denied: no successful tool_result for echo
        results = [e for e in seen if e["type"] == "tool_result"]
        assert all(not r["data"]["ok"] for r in results)
        run = (await c.get(f"/api/runs/{run_id}?token={TOKEN}")).json()
        assert run["status"] == "done"


# --------------------------------------------------------------------------
# missing provider key: honest error, never a faked reply
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_missing_key_is_honest(tmp_path, monkeypatch):
    import agentkai.dashboard.server as server

    class EmptyConfig:
        def key_status(self):
            return {"anthropic": "missing", "gemini": "missing",
                    "openai": "missing", "zhipu": "missing",
                    "ollama": "missing"}

    monkeypatch.setattr(server, "ProviderConfig",
                        type("PC", (), {"load": staticmethod(
                            lambda path=None: EmptyConfig())}))
    app = create_app(TOKEN)  # default factory -> key precheck active
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "hello", "model": "claude"})
                  ).json()["run_id"]
        seen = await collect_sse(c, run_id)
        errors = [e for e in seen if e["type"] == "error"]
        assert errors, "expected an honest error event"
        assert "ANTHROPIC_API_KEY" in errors[0]["data"]["message"]
        run = (await c.get(f"/api/runs/{run_id}?token={TOKEN}")).json()
        assert run["status"] == "error"
        # no fake assistant text was ever emitted
        assert not [e for e in seen if e["type"] == "message_delta"]


# --------------------------------------------------------------------------
# (b) WebChat-style message: gateway -> real Agent -> reply
# --------------------------------------------------------------------------

class FakeWebChat(Channel):
    name = "webchat"

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str]] = []
        self.on_message = None

    def start(self, on_message):
        self.on_message = on_message

    def stop(self):
        pass

    def send_text(self, peer_id, text):
        self.sent.append((peer_id, text))

    def inject(self, peer_id, text):
        self.on_message(InboundMessage(channel=self.name, peer_id=peer_id,
                                       peer_name="tester", text=text,
                                       timestamp=time.time()))


def test_gateway_webchat_to_agent_and_back(tmp_path):
    channel = FakeWebChat()
    events_root = tmp_path / "runs"

    def agent_factory(session_key):
        return Agent(model="fake",
                     client=FakeClient(
                         [LLMMessage(text="hello from the agent")]),
                     gate=PermissionGate(policy={"risk:high": "deny"}),
                     registry=echo_registry(),
                     events_root=str(events_root))

    config = {
        "owner": "webchat:owner1",
        "approval_timeout": 5,
        "model": "fake",
        "identity_links": {},
        "channels": {
            "webchat": {"enabled": True, "allowlist": ["alice", "owner1"],
                        "owner_peer": "owner1"},
        },
    }
    store = SessionStore(path=tmp_path / "sessions.db")
    gw = Gateway(config=config, channels={"webchat": channel},
                 agent_factory=agent_factory, store=store)
    gw.start()
    try:
        channel.inject("alice", "hi agent")
        deadline = time.time() + 20
        while time.time() < deadline and not channel.sent:
            time.sleep(0.2)
        assert channel.sent, "no reply was sent"
        peer, text = channel.sent[0]
        assert peer == "alice"
        assert "hello from the agent" in text
    finally:
        gw.stop()
        store.close()


# --------------------------------------------------------------------------
# (c) scheduled job runs a fake-Agent task through the real loop
# --------------------------------------------------------------------------

def test_scheduler_runs_agent_job(tmp_path):
    def agent_factory(alias):
        return Agent(model="fake",
                     client=FakeClient([LLMMessage(text="job complete")]),
                     gate=PermissionGate(policy={"risk:high": "deny"}),
                     registry=echo_registry(),
                     events_root=str(tmp_path / "runs"))

    sched = Scheduler(db=tmp_path / "scheduler.db",
                      agent_factory=agent_factory)
    sched.add(Job(name="nightly", schedule="0 3 * * *",
                  prompt="summarize the day", model_alias="fake"))
    try:
        out = sched.run_job("nightly")
        assert out["status"] == "done"
        assert "job complete" in out["summary"]
        runs = sched.job_runs("nightly")
        assert runs and runs[0]["status"] == "done"
    finally:
        sched.close()


# --------------------------------------------------------------------------
# (d) subagent spawn/result through the real agent loop
# --------------------------------------------------------------------------

def test_subagent_spawn_and_result(tmp_path):
    gate = PermissionGate(policy={"risk:low": "allow",
                                  "risk:medium": "allow",
                                  "risk:high": "deny"})
    mgr = SubagentManager(
        registry=echo_registry(), gate=gate,
        events_root=str(tmp_path / "runs"),
        client=FakeClient(
            [LLMMessage(text="child task complete")]),
    )
    child_id = mgr.spawn("do the subtask", model_alias="fake")
    try:
        text = mgr.result_text(child_id, timeout=30)
        assert "child task complete" in text
        assert mgr.status(child_id)["status"] == "done"
    finally:
        mgr.wait_all(timeout=10)
