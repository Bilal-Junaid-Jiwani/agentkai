"""Tests for subagent orchestration.

All provider I/O goes through injected fake clients — no network calls.
"""
from __future__ import annotations

import threading

import pytest

from agentkai.events import EventLog, replay
from agentkai.permissions import PermissionGate
from agentkai.providers import LLMMessage, ToolCall
from agentkai.subagents import (
    SubagentManager,
    readonly_subset,
    scoped_registry,
)
from agentkai.tools import Registry, Tool


# -- fakes -------------------------------------------------------------------

class FakeClient:
    """Scripted stand-in for LLMClient.generate (same signature)."""

    def __init__(self, script):
        self.script = list(script)
        self.model = "fake/test-model"

    def generate(self, messages, tools=None, on_delta=None, timeout=None,
                 cancel_event=None, **kwargs):
        item = self.script.pop(0)
        if callable(item):
            return item(messages)
        return item


def final(text: str) -> LLMMessage:
    return LLMMessage(text=text, tool_calls=[])


def tool_call(tool: str, args: dict, call_id: str = "call_1") -> LLMMessage:
    return LLMMessage(text="", tool_calls=[
        ToolCall(id=call_id, name=tool, arguments=args,
                 raw_arguments="{}")])


def make_registry() -> Registry:
    r = Registry()
    r.register(Tool(name="read_file", description="read",
                    json_schema={"type": "object"}, risk="low",
                    func=lambda path: {"lines": ["hi"], "total_lines": 1}))
    r.register(Tool(name="write_file", description="write",
                    json_schema={"type": "object"}, risk="medium",
                    func=lambda path, content: {"ok": True}))
    r.register(Tool(name="exec", description="run",
                    json_schema={"type": "object"}, risk="high",
                    func=lambda command: {"exit_code": 0, "output": "ok"}))
    return r


def make_manager(tmp_path, script, **kw):
    gate = PermissionGate(policy={"risk:low": "allow",
                                  "risk:medium": "allow",
                                  "risk:high": "allow"})
    return SubagentManager(registry=make_registry(), gate=gate,
                           client=FakeClient(script),
                           events_root=tmp_path / "runs", **kw)


# -- scoping -----------------------------------------------------------------

def test_default_subset_is_readonly():
    sub = scoped_registry(make_registry(), None)
    assert sub.names() == ["read_file"]


def test_readonly_subset_helper():
    assert readonly_subset(make_registry()).names() == ["read_file"]


def test_explicit_subset():
    sub = scoped_registry(make_registry(), ["exec", "write_file"])
    assert sorted(sub.names()) == ["exec", "write_file"]


def test_unknown_tool_in_subset_raises():
    with pytest.raises(KeyError):
        scoped_registry(make_registry(), ["nope"])


# -- spawn / wait / result ---------------------------------------------------

def test_spawn_wait_result(tmp_path):
    mgr = make_manager(tmp_path, [final("hello from child")])
    child_id = mgr.spawn("say hi")
    child = mgr.wait(child_id, timeout=10)
    assert child.status == "done"
    assert child.result is not None
    assert child.result.text == "hello from child"
    assert mgr.status(child_id)["status"] == "done"
    assert mgr.result_text(child_id) == "hello from child"


def test_spawn_empty_task_raises(tmp_path):
    mgr = make_manager(tmp_path, [])
    with pytest.raises(ValueError):
        mgr.spawn("   ")


def test_unknown_child_raises(tmp_path):
    mgr = make_manager(tmp_path, [])
    with pytest.raises(KeyError):
        mgr.status("nope")
    with pytest.raises(KeyError):
        mgr.wait("nope", timeout=1)


def test_wait_timeout(tmp_path):
    gate = PermissionGate(policy={"risk:low": "allow"})
    block = threading.Event()

    class BlockingClient(FakeClient):
        def generate(self, *a, **k):
            block.wait(30)
            return final("never")

    mgr = SubagentManager(registry=make_registry(), gate=gate,
                          client=BlockingClient([]),
                          events_root=tmp_path / "runs")
    child_id = mgr.spawn("block")
    with pytest.raises(TimeoutError):
        mgr.wait(child_id, timeout=0.5)
    assert mgr.status(child_id)["status"] == "running"
    mgr.cancel(child_id)
    block.set()
    mgr.wait(child_id, timeout=10)


def test_list_order_and_contents(tmp_path):
    mgr = make_manager(tmp_path, [final("a"), final("b")])
    first = mgr.spawn("task one")
    second = mgr.spawn("task two")
    mgr.wait(first, timeout=10)
    mgr.wait(second, timeout=10)
    listing = mgr.list()
    assert [c["child_id"] for c in listing] == [first, second]
    assert listing[0]["task"] == "task one"
    assert listing[0]["status"] == "done"
    assert listing[0]["run_id"]  # own run id, distinct from parent's


def test_child_gets_own_run_id(tmp_path):
    mgr = make_manager(tmp_path, [final("x"), final("y")])
    a = mgr.spawn("one")
    b = mgr.spawn("two")
    mgr.wait(a, timeout=10)
    mgr.wait(b, timeout=10)
    assert mgr.get(a).result.run_id != mgr.get(b).result.run_id


# -- cancellation ------------------------------------------------------------

def test_cancel_running_child(tmp_path):
    gate = PermissionGate(policy={"risk:low": "allow",
                                  "risk:medium": "allow"})
    r = Registry()
    r.register(Tool(name="slow", description="blocks",
                    json_schema={"type": "object"}, risk="medium",
                    func=lambda: threading.Event().wait(30)))
    mgr = SubagentManager(registry=r, gate=gate,
                          client=FakeClient(
                              [tool_call("slow", {}), final("never")]),
                          events_root=tmp_path / "runs")
    child_id = mgr.spawn("do the slow thing", tools_subset=["slow"])
    import time
    time.sleep(0.5)
    assert mgr.cancel(child_id) is True
    child = mgr.wait(child_id, timeout=10)
    assert child.status == "cancelled"


def test_cancel_finished_child_returns_false(tmp_path):
    mgr = make_manager(tmp_path, [final("done already")])
    child_id = mgr.spawn("quick")
    mgr.wait(child_id, timeout=10)
    assert mgr.cancel(child_id) is False


# -- gate inheritance ----------------------------------------------------------

def test_child_inherits_parent_gate(tmp_path):
    gate = PermissionGate(policy={"risk:low": "allow",
                                  "risk:high": "deny"},
                          ask_callback=lambda action: False)
    mgr = SubagentManager(registry=make_registry(), gate=gate,
                          client=FakeClient(
                              [tool_call("exec", {"command": "rm -rf /"}),
                               final("never")]),
                          events_root=tmp_path / "runs")
    child_id = mgr.spawn("try the dangerous thing", tools_subset=["exec"])
    child = mgr.wait(child_id, timeout=10)
    assert child.agent.gate is gate  # same gate object, inherited
    assert child.status == "done"
    # the denied tool call produced an ERROR tool result, not execution
    tool_msgs = [m for m in child.agent.messages if m.get("role") == "tool"]
    assert tool_msgs and "denied" in tool_msgs[0]["content"]


# -- parent log events -----------------------------------------------------------

def test_parent_log_records_spawn_and_finish(tmp_path):
    parent_log = EventLog(root=tmp_path / "logs")
    mgr = make_manager(tmp_path, [final("child answer")],
                       parent_log=parent_log)
    child_id = mgr.spawn("the task")
    mgr.wait(child_id, timeout=10)
    types = [e.type for e in replay(parent_log.run_id,
                                    root=tmp_path / "logs")]
    assert "subagent_spawn" in types
    assert "subagent_finish" in types


# -- tools exposed to the parent ---------------------------------------------------

def test_subagent_tools_end_to_end(tmp_path):
    mgr = make_manager(tmp_path, [final("tool-spawned answer")])
    registry = Registry()
    mgr.register_tools(registry)
    assert {"spawn_subagent", "subagent_status", "subagent_result",
            "cancel_subagent"} <= set(registry.names())

    out = registry.get("spawn_subagent").run(task="via tool")
    child_id = out["child_id"]
    assert mgr.status(child_id)["status"] in ("running", "done")

    status = registry.get("subagent_status").run(child_id=child_id)
    assert status["child_id"] == child_id

    res = registry.get("subagent_result").run(child_id=child_id, timeout=10)
    assert res["status"] == "done"
    assert res["text"] == "tool-spawned answer"

    cancelled = registry.get("cancel_subagent").run(child_id=child_id)
    assert cancelled == {"child_id": child_id, "cancelled": False}

    assert registry.get("subagent_status").run(
        child_id="bogus").startswith("ERROR:")


def test_spawn_subagent_tool_defaults_to_readonly(tmp_path):
    mgr = make_manager(tmp_path, [final("ok")])
    registry = Registry()
    mgr.register_tools(registry)
    out = registry.get("spawn_subagent").run(task="scoped?")
    child = mgr.wait(out["child_id"], timeout=10)
    assert child.tool_names == ["read_file"]


def test_spawn_subagent_tool_explicit_subset(tmp_path):
    mgr = make_manager(tmp_path, [final("ok")])
    registry = Registry()
    mgr.register_tools(registry)
    out = registry.get("spawn_subagent").run(task="scoped?",
                                             tools="exec, write_file")
    child = mgr.wait(out["child_id"], timeout=10)
    assert sorted(child.tool_names) == ["exec", "write_file"]
