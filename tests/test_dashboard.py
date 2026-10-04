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
from agentkai.memory import Memory
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


def client(memory=None, **kw):
    app = create_app(TOKEN, agent_factory=_fake_factory, memory=memory)
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
async def test_memory_write_endpoint(tmp_path):
    mem = Memory(root=tmp_path / "memory")
    url = "/api/memory/DASH-TEST.md?token=" + TOKEN
    async with client(memory=mem) as c:
        # create: 201, no backup
        r = await c.put(url, json={"content": "# hello\n"})
        assert r.status_code == 201
        body = r.json()
        assert body["created"] is True and body["backup"] is None
        assert body["bytes"] == 8
        assert mem.read("DASH-TEST.md") == "# hello\n"
        # update: 200, previous version kept as .bak
        r = await c.put(url, json={"content": "# hello v2\n"})
        assert r.status_code == 200
        body = r.json()
        assert body["created"] is False
        assert body["backup"] == "DASH-TEST.md.bak"
        assert mem.read("DASH-TEST.md.bak") == "# hello\n"
        # round-trip through the GET reader
        r = await c.get(url)
        assert r.json()["content"] == "# hello v2\n"


@pytest.mark.asyncio
async def test_memory_write_rejects_bad_input(tmp_path):
    mem = Memory(root=tmp_path / "memory")
    async with client(memory=mem) as c:
        r = await c.put("/api/memory/../evil?token=" + TOKEN,
                        json={"content": "x"})
        assert r.status_code in (400, 404)
        r = await c.put("/api/memory/a%5Cb?token=" + TOKEN,
                        json={"content": "x"})
        assert r.status_code == 400
        r = await c.put("/api/memory/BIG.md?token=" + TOKEN,
                        json={"content": "x" * 1_000_001})
        assert r.status_code == 400
        # oversized write must not have touched the store
        assert not (tmp_path / "memory" / "BIG.md").exists()


@pytest.mark.asyncio
async def test_tools_endpoint():
    async with client() as c:
        body = (await c.get(f"/api/tools?token={TOKEN}")).json()
    tools = body["tools"]
    assert isinstance(tools, list) and len(tools) > 0
    names = {t["name"] for t in tools}
    assert {"exec", "read_file", "write_file", "fetch_url"} <= names
    for t in tools:
        assert set(t) == {"name", "description", "risk"}
        assert t["risk"] in ("low", "medium", "high")
        assert t["description"] and isinstance(t["description"], str)


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


# ---------------- scheduler job management (v0.2.0) ----------------

from types import SimpleNamespace

from agentkai.scheduler import Job, Scheduler


class _JobFakeAgent:
    """Fake agent for Scheduler.run_job: instant, scripted, records prompts."""

    def __init__(self):
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(text="job ran fine", status="done",
                               error="", run_id="fake-job-run")


def _sched_client(tmp_path):
    sched = Scheduler(db=tmp_path / "sched-test.db",
                      agent_factory=lambda alias: _JobFakeAgent())
    app = create_app(TOKEN, agent_factory=_fake_factory, scheduler=sched)
    transport = httpx.ASGITransport(app=app)
    return sched, httpx.AsyncClient(transport=transport,
                                   base_url="http://127.0.0.1")


_PAYLOAD = {"name": "morning-brief", "schedule": "*/15 * * * *",
            "prompt": "summarize overnight activity"}


@pytest.mark.asyncio
async def test_scheduler_create_job(tmp_path):
    sched, c = _sched_client(tmp_path)
    async with c:
        r = await c.post(f"/api/scheduler/jobs?token={TOKEN}",
                         json=_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] and body["created"] is True
    assert body["job"]["name"] == "morning-brief"
    assert sched.get("morning-brief").prompt == "summarize overnight activity"
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_create_job_shows_in_list(tmp_path):
    sched, c = _sched_client(tmp_path)
    async with c:
        await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=_PAYLOAD)
        body = (await c.get(f"/api/scheduler/jobs?token={TOKEN}")).json()
    names = [j["name"] for j in body["jobs"]]
    assert "morning-brief" in names
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_create_upserts_existing(tmp_path):
    sched, c = _sched_client(tmp_path)
    async with c:
        await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=_PAYLOAD)
        payload = dict(_PAYLOAD, schedule="0 9 * * *")
        r = await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False
    assert sched.get("morning-brief").schedule == "0 9 * * *"
    assert len(sched.list()) == 1
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_create_validates(tmp_path):
    sched, c = _sched_client(tmp_path)
    bad = [
        dict(_PAYLOAD, schedule="not a cron"),        # bad cron
        dict(_PAYLOAD, schedule="0 9 * *"),            # only 4 fields
        dict(_PAYLOAD, prompt="", command=""),         # nothing to run
        dict(_PAYLOAD, name="   "),                    # empty name
        dict(_PAYLOAD, job_type="weekly"),             # unknown type
        dict(_PAYLOAD, job_type="at", schedule="0 9 * * *"),  # at needs @at:
    ]
    async with c:
        for payload in bad:
            r = await c.post(f"/api/scheduler/jobs?token={TOKEN}",
                             json=payload)
            assert r.status_code == 400, (payload, r.text)
    assert sched.list() == []
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_create_one_shot(tmp_path):
    sched, c = _sched_client(tmp_path)
    payload = dict(_PAYLOAD, name="once", job_type="at",
                   schedule="@at:2026-10-05T09:00")
    async with c:
        r = await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=payload)
    assert r.status_code == 201, r.text
    assert sched.get("once").job_type == "at"
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_delete(tmp_path):
    sched, c = _sched_client(tmp_path)
    async with c:
        await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=_PAYLOAD)
        r = await c.delete(
            f"/api/scheduler/jobs/morning-brief?token={TOKEN}")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        r2 = await c.delete(
            f"/api/scheduler/jobs/morning-brief?token={TOKEN}")
        assert r2.status_code == 404
    assert sched.get("morning-brief") is None
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_run_job_now(tmp_path):
    sched, c = _sched_client(tmp_path)
    async with c:
        await c.post(f"/api/scheduler/jobs?token={TOKEN}", json=_PAYLOAD)
        r = await c.post(
            f"/api/scheduler/jobs/morning-brief/run?token={TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert "job ran fine" in body["summary"]
    runs = sched.job_runs("morning-brief")
    assert len(runs) == 1 and runs[0]["status"] == "done"
    assert sched.get("morning-brief").last_run
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_run_unknown_and_disabled(tmp_path):
    sched, c = _sched_client(tmp_path)
    sched.add(Job(name="off", schedule="* * * * *", prompt="x",
                  enabled=False))
    async with c:
        r = await c.post(f"/api/scheduler/jobs/nope/run?token={TOKEN}")
        assert r.status_code == 404
        r = await c.post(f"/api/scheduler/jobs/off/run?token={TOKEN}")
        assert r.status_code == 400
    sched.close()


@pytest.mark.asyncio
async def test_scheduler_endpoints_require_token(tmp_path):
    _sched, c = _sched_client(tmp_path)
    async with c:
        r = await c.post("/api/scheduler/jobs", json=_PAYLOAD)
        assert r.status_code == 401
        r = await c.delete("/api/scheduler/jobs/x")
        assert r.status_code == 401
        r = await c.post("/api/scheduler/jobs/x/run")
        assert r.status_code == 401
