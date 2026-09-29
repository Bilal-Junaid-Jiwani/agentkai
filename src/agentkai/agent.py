"""Hardened ReAct agent loop: think -> tool-call -> observe -> repeat.

Provider-agnostic: works with any model behind :class:`LLMClient`, as long
as it supports function calling (Claude, Gemini, GPT, most others; small
local models vary — see research/ARCHITECTURE.md).

Hardening over the v0 scaffold:
- streaming responses with incremental text deltas (``on_text`` callback)
- tool calls normalized to one :class:`ToolCall` dataclass across providers
- per-step timeout, total run timeout, cooperative cancellation
- context-window budget: oldest tool outputs are truncated first, then
  compacted into a short summary
- max-iteration guard
- permission gate (allow/ask/deny) consulted before every tool call
- every step recorded to an append-only JSONL event log
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import litellm

from .events import Event, EventLog
from .permissions import Action, PermissionGate
from .providers import LLMClient, ProviderConfig, ToolCall
from .tools import default_registry

SYSTEM_PROMPT = """You are agentkai, a personal AI assistant running on the \
user's own machine. You have tools: use them to get things done instead of \
guessing. Be concise, honest about uncertainty, and never invent file contents, \
URLs, or results — read them with your tools first."""

# Rough token estimate when the provider can't count (chars/4 is the
# standard heuristic; ARCHITECTURE.md notes provider counters are 10-30% off
# for non-OpenAI models, so this only drives compaction, never hard limits).
CHARS_PER_TOKEN = 4


@dataclass
class RunResult:
    run_id: str
    text: str
    status: str  # done | max_iterations | timeout | cancelled | error
    iterations: int
    events_path: Path
    error: str = ""


def estimate_tokens(messages: list[dict], model: str) -> int:
    """Best-effort token count for a message list.

    Tries LiteLLM's counter first; falls back to chars/4. Used only for
    context-budget compaction — never for billing or hard provider limits.
    """
    try:
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception:
        total = 0
        for m in messages:
            total += len(str(m.get("content", "")))
            for tc in m.get("tool_calls", []) or []:
                fn = (tc.get("function", {}) if isinstance(tc, dict)
                      else getattr(tc, "function", None))
                if isinstance(fn, dict):
                    total += len(str(fn.get("arguments", "")))
                elif fn is not None:
                    total += len(str(getattr(fn, "arguments", "")))
        return total // CHARS_PER_TOKEN


class Agent:
    """The hardened agent loop."""

    def __init__(
        self,
        model: str = "claude",
        system_prompt: str = SYSTEM_PROMPT,
        registry=None,
        gate: PermissionGate | None = None,
        client: LLMClient | None = None,
        config: ProviderConfig | None = None,
        max_iterations: int = 25,
        step_timeout: float = 180,
        total_timeout: float = 1200,
        tool_timeout: float = 300,
        max_context_tokens: int = 100_000,
        events_root: str | Path | None = None,
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[Event], None] | None = None,
        **client_kwargs,
    ):
        self.system_prompt = system_prompt
        self.registry = registry or default_registry()
        self.gate = gate or PermissionGate()
        self.config = config or ProviderConfig.load()
        self.client = client or LLMClient(model, config=self.config,
                                         **client_kwargs)
        self.model_name = self.client.model
        self.max_iterations = max_iterations
        self.step_timeout = step_timeout
        self.total_timeout = total_timeout
        self.tool_timeout = tool_timeout
        self.max_context_tokens = max_context_tokens
        self.events_root = events_root
        self.on_text = on_text
        self.on_event = on_event
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # -- public API ------------------------------------------------------

    def run(self, prompt: str,
            cancel_event: threading.Event | None = None,
            on_text: Callable[[str], None] | None = None) -> RunResult:
        """Run the ReAct loop to completion. Returns a RunResult."""
        cancel = cancel_event or threading.Event()
        emit = on_text or self.on_text
        log = EventLog(root=self.events_root, on_event=self.on_event)
        deadline = time.monotonic() + self.total_timeout
        self.messages.append({"role": "user", "content": prompt})
        log.record("run_start", model=self.model_name, prompt=prompt,
                   max_iterations=self.max_iterations)

        def cancelled() -> bool:
            return cancel.is_set() or time.monotonic() >= deadline

        status, final_text, error = "done", "", ""
        iterations = 0
        try:
            for i in range(self.max_iterations):
                iterations = i + 1
                if cancel.is_set():
                    status = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    status = "timeout"
                    error = (f"total run timeout ({self.total_timeout}s) "
                             "exceeded")
                    break
                self._enforce_budget()
                remaining = max(1.0, deadline - time.monotonic())
                step_to = min(self.step_timeout, remaining)
                try:
                    msg = self._llm_step(step_to, cancel, emit)
                except TimeoutError as exc:
                    status = "timeout"
                    error = str(exc)
                    break
                log.record("llm_message", text=msg.text,
                           tool_calls=[{"id": tc.id, "name": tc.name,
                                        "arguments": tc.arguments}
                                       for tc in msg.tool_calls])
                # (text deltas were already streamed via on_text during the step)
                if not msg.tool_calls:
                    final_text = msg.text
                    status = "done"
                    break
                self.messages.append({
                    "role": "assistant",
                    "content": msg.text,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": tc.raw_arguments
                                      or "{}"}}
                        for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    if cancelled():
                        status = "cancelled" if cancel.is_set() else "timeout"
                        break
                    self._exec_tool(tc, log, cancel)
                else:
                    continue
                break
            else:
                status = "max_iterations"
                error = (f"stopped after {self.max_iterations} iterations "
                         "without a final answer")
                final_text = error
        except Exception as exc:  # noqa: BLE001 - run must always report
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            final_text = error
        finally:
            log.record("run_end", status=status, final_text=final_text[:2000],
                       error=error, iterations=iterations)
            events_path = log.path
            log.close()
        return RunResult(run_id=log.run_id, text=final_text, status=status,
                         iterations=iterations, events_path=events_path,
                         error=error)

    def chat(self, text: str) -> str:
        """Non-streaming convenience: run one prompt, return final text."""
        return self.run(text).text

    # -- one LLM step with timeout ----------------------------------------

    def _llm_step(self, timeout: float, cancel: threading.Event,
                  emit: Callable[[str], None] | None):
        """Run client.generate in a worker thread; bound by ``timeout``.

        The worker is a daemon: on timeout the agent moves on (the stuck
        network read is abandoned, not leaked into the loop).
        """
        box: dict = {}

        def _target():
            try:
                box["msg"] = self.client.generate(
                    self.messages, tools=self.registry.schemas(),
                    on_delta=emit, cancel_event=cancel)
            except Exception as exc:  # noqa: BLE001
                box["exc"] = exc

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"LLM step timed out after {timeout:.0f}s")
        if "exc" in box:
            # Preserve cancellation/timeout semantics from the client.
            raise box["exc"]
        if "msg" not in box:
            raise RuntimeError("LLM step produced no response")
        return box["msg"]

    # -- tool execution with permission gate --------------------------------

    def _exec_tool(self, tc: ToolCall, log: EventLog,
                   cancel: threading.Event) -> None:
        log.record("tool_call", id=tc.id, name=tc.name,
                   arguments=tc.arguments)
        try:
            tool = self.registry.get(tc.name)
        except KeyError:
            result = f"ERROR: unknown tool {tc.name!r}"
            ok = False
        else:
            action = Action(tool=tc.name, args=tc.arguments, risk=tool.risk)
            decision = self.gate.check(action)
            log.record("approval", tool=tc.name, decision=decision,
                       risk=tool.risk)
            if decision != "allow":
                result = (f"ERROR: permission gate denied {tc.name}: "
                          f"decision={decision}")
                ok = False
            else:
                result, ok = self._run_tool_guarded(tool, tc.arguments,
                                                    cancel)
        if not isinstance(result, str):
            import json as _json
            result = _json.dumps(result, default=str)
        log.record_tool_result(tc.id, tc.name, result, ok=ok)
        self.messages.append({"role": "tool", "tool_call_id": tc.id,
                              "name": tc.name, "content": result})

    def _run_tool_guarded(self, tool, args: dict,
                          cancel: threading.Event) -> tuple[str, bool]:
        """Run a tool in a worker thread with a timeout. Tools report
        errors as strings; unexpected exceptions become ERROR strings."""
        box: dict = {}

        def _target():
            try:
                box["out"] = tool.run(**args)
            except Exception as exc:  # noqa: BLE001
                box["out"] = f"ERROR: {type(exc).__name__}: {exc}"

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        # Cooperative cancellation: poll in small slices.
        waited = 0.0
        while t.is_alive() and waited < self.tool_timeout:
            if cancel.is_set():
                return "ERROR: cancelled by user", False
            time.sleep(0.05)
            waited += 0.05
        if t.is_alive():
            return (f"ERROR: tool {tool.name} timed out after "
                    f"{self.tool_timeout:.0f}s", False)
        out = box.get("out", "ERROR: tool produced no output")
        ok = not (isinstance(out, str) and out.startswith("ERROR:"))
        return out, ok

    # -- context budget -----------------------------------------------------

    def _enforce_budget(self) -> None:
        """Keep estimated tokens under budget.

        1. Truncate the oldest tool outputs first (keep the newest two
           intact — the model usually needs those).
        2. If still over budget, compact older tool exchanges into a short
           deterministic summary.
        """
        if estimate_tokens(self.messages, self.model_name) \
                <= self.max_context_tokens:
            return
        tool_msgs = [m for m in self.messages if m.get("role") == "tool"]
        # Phase 1: truncate oldest tool outputs, newest two untouched.
        for m in tool_msgs[:-2]:
            content = str(m.get("content", ""))
            if len(content) > 800:
                m["content"] = (content[:800]
                                + f"\n... [truncated "
                                f"{len(content) - 800} chars for context]")
        if estimate_tokens(self.messages, self.model_name) \
                <= self.max_context_tokens:
            return
        # Phase 2: compact all but the last 4 tool exchanges into a summary.
        keep_ids = {id(m) for m in tool_msgs[-4:]}
        drop = [m for m in tool_msgs if id(m) not in keep_ids]
        if drop:
            lines = []
            for m in drop:
                first = str(m.get("content", "")).split("\n")[0][:160]
                lines.append(f"- {m.get('name')}: {first}")
            summary = ("[earlier tool activity compacted for context: "
                       f"{len(drop)} calls]\n" + "\n".join(lines))
            # Replace the dropped messages' content in place (keeps ids).
            for m in drop:
                m["content"] = "[compacted]"
            # Insert the summary right before the first kept tool message.
            kept = [m for m in tool_msgs if id(m) in keep_ids]
            idx = self.messages.index(kept[0]) if kept else len(self.messages)
            self.messages.insert(idx, {"role": "user",
                                       "content": summary})
