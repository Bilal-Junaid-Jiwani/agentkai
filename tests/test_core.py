"""Tests for the hardened core: agent loop, providers, permissions, events.

All provider I/O goes through an injected FakeClient — no network calls.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from agentkai.agent import Agent, estimate_tokens
from agentkai.events import EventLog, list_runs, replay, summarize
from agentkai.permissions import Action, PermissionGate
from agentkai.providers import (
    LLMClient,
    LLMMessage,
    ToolCall,
    normalize_tool_calls,
    resolve_fallbacks,
    resolve_model,
)
from agentkai.tools import Registry, Tool


# -- fakes -------------------------------------------------------------------

class FakeClient:
    """Scripted stand-in for LLMClient.generate (same signature)."""

    def __init__(self, script):
        self.script = list(script)
        self.model = "fake/test-model"
        self.seen_messages: list = []

    def generate(self, messages, tools=None, on_delta=None, timeout=None,
                 cancel_event=None, **kwargs):
        self.seen_messages.append(list(messages))
        item = self.script.pop(0)
        if callable(item):
            return item(messages)
        if on_delta and item.text:
            on_delta(item.text)
        return item


def scripted_tool_call(tool: str, args: dict, call_id: str = "call_1"):
    return LLMMessage(text="", tool_calls=[
        ToolCall(id=call_id, name=tool, arguments=args,
                 raw_arguments=json.dumps(args))])


def make_registry(spy: dict) -> Registry:
    r = Registry()

    def _read(path: str) -> dict:
        spy.setdefault("read_file", []).append(path)
        return {"lines": ["hello world"], "total_lines": 1}

    def _exec(command: str) -> dict:
        spy.setdefault("exec", []).append(command)
        return {"exit_code": 0, "output": "ok"}

    r.register(Tool(name="read_file", description="read a file",
                    json_schema={"type": "object",
                                 "properties": {"path": {"type": "string"}}},
                    risk="low", func=_read))
    r.register(Tool(name="exec", description="run a command",
                    json_schema={"type": "object",
                                 "properties": {"command": {"type": "string"}}},
                    risk="high", func=_exec))
    return r


def make_agent(script, tmp_path, spy=None, gate=None, **kw):
    spy = {} if spy is None else spy
    gate = PermissionGate(policy={"risk:low": "allow",
                                  "risk:medium": "allow",
                                  "risk:high": "allow"}) if gate is None else gate
    return Agent(model="fake", registry=make_registry(spy), gate=gate,
                 client=FakeClient(script), events_root=tmp_path / "runs",
                 **kw)


# -- 1. tool-call loop --------------------------------------------------------

def test_tool_call_loop(tmp_path):
    spy: dict = {}
    agent = make_agent(
        [scripted_tool_call("read_file", {"path": "/tmp/x.txt"}),
         LLMMessage(text="The file says hello world.")],
        tmp_path, spy)
    result = agent.run("read the file")
    assert result.status == "done"
    assert result.text == "The file says hello world."
    assert spy["read_file"] == ["/tmp/x.txt"]
    # the tool result was fed back into the conversation
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "hello world" in tool_msgs[0]["content"]


def test_unknown_tool_is_reported_not_raised(tmp_path):
    agent = make_agent(
        [scripted_tool_call("nope", {}),
         LLMMessage(text="recovered")],
        tmp_path)
    result = agent.run("do it")
    assert result.status == "done"
    assert result.text == "recovered"
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert "unknown tool" in tool_msgs[0]["content"]


def test_max_iterations_guard(tmp_path):
    agent = make_agent(
        [scripted_tool_call("read_file", {"path": "x"})] * 10,
        tmp_path, max_iterations=3)
    result = agent.run("loop forever")
    assert result.status == "max_iterations"
    assert result.iterations == 3


# -- 2. timeout ---------------------------------------------------------------

def test_step_timeout(tmp_path):
    def slow(messages):
        time.sleep(5)
        return LLMMessage(text="too late")

    agent = make_agent([slow], tmp_path, step_timeout=0.3)
    result = agent.run("slow model")
    assert result.status == "timeout"
    assert "timed out" in result.error


# -- 3. approval gates ----------------------------------------------------------

def test_approval_deny_path(tmp_path):
    spy: dict = {}
    gate = PermissionGate(policy={"tool:exec": "deny"})
    agent = make_agent(
        [scripted_tool_call("exec", {"command": "rm -rf /"}),
         LLMMessage(text="I will not do that.")],
        tmp_path, spy, gate=gate)
    result = agent.run("delete everything")
    assert result.status == "done"
    assert "exec" not in spy  # tool never ran
    decisions = gate.audit_log()
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "deny"
    assert decisions[0]["tool"] == "exec"
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert "denied" in tool_msgs[0]["content"]


def test_approval_ask_declined(tmp_path):
    spy: dict = {}
    gate = PermissionGate(
        policy={"risk:high": "ask"},
        ask_callback=lambda action: False)
    agent = make_agent(
        [scripted_tool_call("exec", {"command": "ls"}),
         LLMMessage(text="ok")],
        tmp_path, spy, gate=gate)
    result = agent.run("list files")
    assert result.status == "done"
    assert "exec" not in spy
    assert gate.audit_log()[0]["decision"] == "deny"
    assert gate.audit_log()[0]["approved"] is False


def test_approval_ask_approved(tmp_path):
    spy: dict = {}
    seen = {}

    def approve(action):
        seen["action"] = action
        return True

    gate = PermissionGate(policy={"risk:high": "ask"}, ask_callback=approve)
    agent = make_agent(
        [scripted_tool_call("exec", {"command": "echo hi"}),
         LLMMessage(text="done")],
        tmp_path, spy, gate=gate)
    result = agent.run("say hi")
    assert result.status == "done"
    assert spy["exec"] == ["echo hi"]
    assert isinstance(seen["action"], Action)
    assert gate.audit_log()[0]["decision"] == "allow"


def test_gate_as_adapter_for_tool_run(tmp_path):
    """PermissionGate.as_gate_fn() plugs into Tool.run(_gate=...)."""
    from agentkai.tools import ApprovalRequired, PermissionDenied
    gate = PermissionGate(policy={"tool:exec": "deny"})
    registry = make_registry({})
    with pytest.raises(PermissionDenied):
        registry.get("exec").run(_gate=gate.as_gate_fn(), command="ls")
    gate2 = PermissionGate(policy={"risk:high": "ask"},
                           ask_callback=lambda a: False)
    with pytest.raises(ApprovalRequired):
        registry.get("exec").run(_gate=gate2.as_gate_fn(), command="ls")


# -- 4. event log replay ---------------------------------------------------------

def test_event_log_replay(tmp_path):
    spy: dict = {}
    agent = make_agent(
        [scripted_tool_call("read_file", {"path": "/tmp/x.txt"}),
         LLMMessage(text="hello world")],
        tmp_path, spy)
    result = agent.run("read the file")
    types = [e.type for e in replay(result.run_id, tmp_path / "runs")]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"
    assert "llm_message" in types
    assert "tool_call" in types
    assert "approval" in types
    assert "tool_result" in types
    s = summarize(result.run_id, tmp_path / "runs")
    assert s["status"] == "done"
    assert s["tool_calls"] == ["read_file"]
    assert s["approvals"] == ["read_file:allow"]


def test_event_log_isolation_between_runs(tmp_path):
    agent = make_agent([LLMMessage(text="one")], tmp_path)
    r1 = agent.run("first")
    agent2 = make_agent([LLMMessage(text="two")], tmp_path)
    r2 = agent2.run("second")
    assert r1.run_id != r2.run_id
    assert {r1.run_id, r2.run_id} <= set(list_runs(tmp_path / "runs"))


def test_event_log_truncates_huge_results(tmp_path):
    log = EventLog(root=tmp_path / "runs")
    big = "x" * 10_000
    log.record_tool_result("c1", "read_file", big)
    log.close()
    events = list(replay(log.run_id, tmp_path / "runs"))
    assert events[0].data["truncated"] is True
    assert len(events[0].data["output"]) < 10_000


def test_event_seq_resumes_on_reopen(tmp_path):
    log = EventLog(run_id="r1", root=tmp_path / "runs")
    log.record("run_start")
    log.close()
    log2 = EventLog(run_id="r1", root=tmp_path / "runs")
    log2.record("run_end")
    log2.close()
    seqs = [e.seq for e in replay("r1", tmp_path / "runs")]
    assert seqs == [1, 2]


# -- 5. aliases -------------------------------------------------------------------

def test_alias_resolution():
    assert resolve_model("claude") == "anthropic/claude-sonnet-4-6"
    assert resolve_model("CLAUDE") == "anthropic/claude-sonnet-4-6"
    assert resolve_model("gemini") == "gemini/gemini-2.5-flash"
    assert resolve_model("gpt") == "gpt-4o"
    assert resolve_model("glm") == "zhipu/glm-4-plus"
    assert resolve_model("local") == "ollama/qwen3:32b"
    # unknown names pass through untouched
    assert resolve_model("ollama/llama3.1") == "ollama/llama3.1"
    assert resolve_model("openrouter/foo/bar") == "openrouter/foo/bar"


def test_alias_fallbacks():
    fallbacks = resolve_fallbacks("claude")
    assert fallbacks  # non-empty chain
    assert all(isinstance(m, str) for m in fallbacks)
    assert "anthropic/claude-sonnet-4-6" not in fallbacks  # no self-retry
    assert resolve_fallbacks("local") == []


def test_custom_aliases_override():
    assert resolve_model("work", {"work": "openai/gpt-4o"}) == "openai/gpt-4o"


class _TextChunk:
    """Minimal streaming chunk shape for LLMClient._generate_once."""

    def __init__(self, text: str):
        from types import SimpleNamespace
        self.choices = [SimpleNamespace(
            delta=SimpleNamespace(content=text, tool_calls=None),
            message=None)]
        self.usage = None


def test_generate_retries_entire_fallback_chain():
    """A failure on the first fallback must not abort the rest of the chain.

    Regression: the loop used to retry only after the primary failed, so a
    down first fallback raised without ever trying the remaining models.
    """
    tried: list[str] = []

    def flaky(model, messages, tools=None, stream=True, **kwargs):
        tried.append(model)
        if model == "gpt-4o":  # last model in claude's chain
            return iter([_TextChunk("ok from fallback")])
        raise RuntimeError(f"provider down: {model}")

    client = LLMClient(model="claude", completion_fn=flaky)
    assert len(client.models_tried()) >= 3  # primary + >=2 fallbacks
    msg = client.generate([{"role": "user", "content": "hi"}])
    assert msg.text == "ok from fallback"
    assert tried == client.models_tried()  # the whole chain was walked


def test_generate_raises_after_chain_exhausted():
    """When every model fails, the error names every model that was tried."""
    def always_down(model, messages, tools=None, stream=True, **kwargs):
        raise RuntimeError(f"provider down: {model}")

    client = LLMClient(model="claude", completion_fn=always_down)
    with pytest.raises(RuntimeError, match="all models failed") as exc_info:
        client.generate([{"role": "user", "content": "hi"}])
    for model in client.models_tried():
        assert model in str(exc_info.value)


def test_generate_without_fallbacks_raises_on_first_failure():
    """Aliases with an empty chain (e.g. "local") fail fast, unchanged."""
    def down(model, messages, tools=None, stream=True, **kwargs):
        raise RuntimeError("provider down")

    client = LLMClient(model="local", completion_fn=down)
    assert client.models_tried() == ["ollama/qwen3:32b"]
    with pytest.raises(RuntimeError, match="all models failed"):
        client.generate([{"role": "user", "content": "hi"}])


# -- 6. normalization --------------------------------------------------------------

def test_normalize_openai_shape():
    msg = {"tool_calls": [
        {"id": "abc", "type": "function",
         "function": {"name": "read_file",
                      "arguments": '{"path": "a.txt"}'}}]}
    calls = normalize_tool_calls(msg)
    assert len(calls) == 1
    assert calls[0].id == "abc"
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}


def test_normalize_legacy_function_call():
    msg = {"function_call": {"name": "exec",
                             "arguments": '{"command": "ls"}'}}
    calls = normalize_tool_calls(msg)
    assert len(calls) == 1
    assert calls[0].name == "exec"


def test_normalize_bad_json_never_raises():
    msg = {"tool_calls": [
        {"id": "x", "function": {"name": "exec",
                                "arguments": "{not json"}}]}
    calls = normalize_tool_calls(msg)
    assert calls[0].arguments == {}
    assert calls[0].raw_arguments == "{not json"


# -- 7. cancellation + misc ----------------------------------------------------------

def test_cancellation_before_start(tmp_path):
    cancel = threading.Event()
    cancel.set()
    agent = make_agent([LLMMessage(text="never")], tmp_path)
    result = agent.run("hello", cancel_event=cancel)
    assert result.status == "cancelled"


def test_streaming_deltas_reach_callback(tmp_path):
    seen: list[str] = []
    agent = make_agent([LLMMessage(text="hel" + "lo")], tmp_path)
    agent.run("hi", on_text=seen.append)
    assert "".join(seen) == "hello"


def test_estimate_tokens_falls_back_without_network():
    msgs = [{"role": "user", "content": "x" * 400}]
    n = estimate_tokens(msgs, "fake/model")
    assert isinstance(n, int) and n > 0


def test_context_budget_truncates_oldest_first(tmp_path):
    agent = make_agent([LLMMessage(text="ok")], tmp_path,
                       max_context_tokens=50)
    big = "y" * 5000
    agent.messages.append({"role": "tool", "tool_call_id": "c1",
                           "name": "read_file", "content": big})
    agent.messages.append({"role": "tool", "tool_call_id": "c2",
                           "name": "read_file", "content": big})
    agent.messages.append({"role": "tool", "tool_call_id": "c3",
                           "name": "read_file", "content": big})
    agent._enforce_budget()
    contents = [m["content"] for m in agent.messages
                if m.get("role") == "tool"]
    assert any("truncated" in c or "compacted" in c for c in contents)
