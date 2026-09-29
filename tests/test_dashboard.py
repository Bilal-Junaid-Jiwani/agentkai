"""Dashboard tests: auth, localhost-only bind, API contract smoke tests.

Chat/run tests inject a fake agent factory (scripted provider, real agent
loop) — the default factory requires real provider keys. Full end-to-end
coverage lives in tests/test_integration.py.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path

import httpx
import pytest

from agentkai.agent import Agent
from agentkai.dashboard import DASHBOARD_PORT, run
from agentkai.dashboard.server import create_app
from agentkai.permissions import PermissionGate
from agentkai.providers import LLMMessage
from agentkai.tools import Registry, Tool

STATIC = Path(__file__).parent.parent / "src" / "agentkai" / "dashboard" / "static"
TOKEN = "test-token-abc123"


class _FakeClient:
    model = "fake/test-model"

    def __init__(self, text="hello from the real loop"):
        self.text = text
        self._lock = threading.Lock()
        self._done = False

    def generate(self, messages, tools=None, on_delta=None, timeout=None,
                 cancel_event=None, **kwargs):
        with self._lock:
            first = not self._done
            self._done = True
        if first and on_delta and self.text:
            for j in range(0, len(self.text), 6):
                on_delta(self.text[j:j + 6])
        return LLMMessage(text=self.text if first else "")


def _fake_factory(model_alias, gate):
    reg = Registry()
    reg.register(Tool(name="echo", description="echo",
                      json_schema={"type": "object", "properties": {}},
                      risk="low", func=lambda: "echo-ok"))
    return Agent(model="fake", client=_FakeClient(), gate=gate, registry=reg,
                 events_root=tempfile.mkdtemp(prefix="ak-dash-test-"))


def client(**kw):
    app = create_app(TOKEN, agent_factory=_fake_factory)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                             **kw)


# ---------------- auth ----------------

@pytest.mark.asyncio
async def test_no_token_is_401():
    async with client() as c:
        r = await c.get("/api/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_is_401():
    async with client() as c:
        r = await c.get("/api/status?token=nope")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_query_token_ok():
    async with client() as c:
        r = await c.get(f"/api/status?token={TOKEN}")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_bearer_header_ok():
    async with client() as c:
        r = await c.get("/api/status",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_static_shell_is_public_but_api_is_not():
    # the static UI shell (css/js/img, no data) must load without a token
    # so the page can bootstrap; everything with data stays token-gated
    async with client() as c:
        r = await c.get("/static/app.js")
        assert r.status_code == 200
        assert (await c.get("/")).status_code == 401
        assert (await c.get("/api/status")).status_code == 401


@pytest.mark.asyncio
async def test_tokens_differ_per_launch():
    a, b = create_app(), create_app()
    assert a.state.dashboard_token != b.state.dashboard_token


# ---------------- localhost-only bind ----------------

def test_refuses_non_localhost_bind():
    with pytest.raises(RuntimeError, match="refusing to bind"):
        run(host="0.0.0.0")
    with pytest.raises(RuntimeError, match="refusing to bind"):
        run(host="192.168.1.10")


def test_default_port():
    assert DASHBOARD_PORT == 8931


# ---------------- API contract ----------------

@pytest.mark.asyncio
async def test_status_shape():
    async with client() as c:
        r = await c.get(f"/api/status?token={TOKEN}")
    body = r.json()
    for key in ("ok", "version", "uptime_s", "model_default", "runs_active"):
        assert key in body, key


@pytest.mark.asyncio
async def test_runs_empty_then_chat_creates_one():
    async with client() as c:
        assert (await c.get(f"/api/runs?token={TOKEN}")).json() == {"runs": []}
        r = await c.post(f"/api/chat?token={TOKEN}",
                         json={"prompt": "hello"})
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        assert run_id
        runs = (await c.get(f"/api/runs?token={TOKEN}")).json()["runs"]
        assert len(runs) == 1 and runs[0]["id"] == run_id
        # the real loop runs in the background; it may already be done
        assert runs[0]["status"] in ("running", "done")


@pytest.mark.asyncio
async def test_chat_rejects_empty_prompt():
    async with client() as c:
        r = await c.post(f"/api/chat?token={TOKEN}", json={"prompt": "  "})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_sse_streams_run_to_finish():
    async with client() as c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "stream me"})).json()["run_id"]
        seen, finished = [], False
        async with c.stream(
                "GET", f"/api/events?token={TOKEN}&run_id={run_id}",
                timeout=30) as s:
            assert s.status_code == 200
            assert s.headers["content-type"].startswith("text/event-stream")
            async for line in s.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    seen.append(ev["type"])
                    if ev["type"] == "run_finished":
                        finished = True
                        break
        assert finished
        assert "run_started" in seen
        assert any(t == "message_delta" for t in seen)
        # replay endpoint has the same events afterwards
        replay = (await c.get(
            f"/api/runs/{run_id}/events?token={TOKEN}")).json()["events"]
        assert [e["type"] for e in replay] == seen


@pytest.mark.asyncio
async def test_models_endpoint():
    async with client() as c:
        body = (await c.get(f"/api/models?token={TOKEN}")).json()
    assert body["default"] == "claude"
    aliases = {m["alias"]: m["resolved"] for m in body["models"]}
    assert aliases["claude"] == "anthropic/claude-sonnet-4-6"
    assert aliases["local"] == "ollama/qwen3:32b"
    assert set(body["key_status"]) >= {"anthropic", "gemini", "openai"}
    # key presence only — never values
    assert all(v in ("set", "missing")
               for v in body["key_status"].values())


@pytest.mark.asyncio
async def test_approve_rejects_without_pending():
    async with client() as c:
        run_id = (await c.post(f"/api/chat?token={TOKEN}",
                               json={"prompt": "hi"})).json()["run_id"]
        r = await c.post(f"/api/runs/{run_id}/approve?token={TOKEN}",
                         json={"action_id": "x", "approved": True})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_unknown_run_404s():
    async with client() as c:
        assert (await c.get("/api/runs/nope/events?token=" + TOKEN)
                ).status_code == 404
        assert (await c.get("/api/events?token=" + TOKEN + "&run_id=nope")
                ).status_code == 404


@pytest.mark.asyncio
async def test_memory_endpoints():
    async with client() as c:
        body = (await c.get(f"/api/memory?token={TOKEN}")).json()
        names = [f["name"] for f in body["files"]]
        assert "MEMORY.md" in names and "SOUL.md" in names
        assert (await c.get(f"/api/memory/../x?token={TOKEN}")).status_code in (400, 404)
        r = await c.get(f"/api/memory/NOPE.md?token={TOKEN}")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_scheduler_jobs_endpoint():
    async with client() as c:
        body = (await c.get(f"/api/scheduler/jobs?token={TOKEN}")).json()
    assert "jobs" in body and "db" in body
    assert isinstance(body["jobs"], list)


# ---------------- offline discipline ----------------

def test_no_external_urls_in_static():
    offenders = []
    for p in STATIC.rglob("*"):
        if p.is_file() and p.suffix in (".html", ".js", ".css"):
            text = p.read_text()
            for needle in ("http://", "https://", "//cdn", "googleapis"):
                if needle in text:
                    offenders.append(f"{p.name}: {needle}")
    assert not offenders, offenders
