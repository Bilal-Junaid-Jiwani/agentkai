"""Subagent orchestration: delegate work to child agents in background threads.

A subagent is a full :class:`agentkai.agent.Agent` running in its own thread
with:

- its own run id and event log (``~/.agentkai/runs/<run_id>/``), so every
  child is independently replayable;
- a *scoped* tool subset (default: read-only tools), so a child cannot do
  more than its parent allowed;
- the parent's permission gate (inherited, never widened);
- a non-interactive approval policy: a background thread must never block
  on a terminal prompt, so "ask" decisions resolve to deny unless the caller
  overrides the gate.

The parent's event log records ``subagent_spawn`` and ``subagent_finish``
events, and the parent can expose the children as tools
(``spawn_subagent``, ``subagent_status``, ``subagent_result``,
``cancel_subagent``).

Everything here is thread-safe: all manager state is guarded by one lock.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .agent import Agent, RunResult
from .events import EventLog
from .permissions import PermissionGate
from .tools import Registry, Tool

CHILD_SYSTEM_PROMPT = """You are a subagent of agentkai, a personal AI \
assistant. You were spawned to complete one specific task. Do the task and \
report the result concisely — do not ask clarifying questions, do not start \
side work, and do not invent facts: use your tools. When you are done, give \
a complete final answer, because that answer is the only thing your parent \
will see."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def readonly_subset(registry: Registry) -> Registry:
    """A new registry holding only the registry's low-risk (read-only) tools."""
    sub = Registry()
    for name in registry.names():
        tool = registry.get(name)
        if tool.risk == "low":
            sub.register(tool)
    return sub


def scoped_registry(registry: Registry,
                    tools_subset: Sequence[str] | None) -> Registry:
    """Build the tool registry a child agent may use.

    ``tools_subset`` is a list of tool names from the parent registry.
    ``None`` means the safe default: low-risk tools only. Unknown names
    raise :class:`KeyError` so misconfigurations fail loudly.
    """
    if tools_subset is None:
        return readonly_subset(registry)
    sub = Registry()
    for name in tools_subset:
        sub.register(registry.get(name))  # KeyError on unknown name
    return sub


@dataclass
class ChildRun:
    """Bookkeeping for one spawned child."""
    child_id: str
    task: str
    model_alias: str
    tool_names: list[str]
    agent: Agent = field(repr=False)
    cancel_event: threading.Event = field(repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    status: str = "running"  # running | done | max_iterations | timeout | cancelled | error
    result: RunResult | None = None
    started_ts: str = field(default_factory=_utcnow)
    finished_ts: str = ""

    def to_dict(self) -> dict:
        return {
            "child_id": self.child_id,
            "task": self.task[:200],
            "model": self.model_alias,
            "tools": self.tool_names,
            "status": self.status,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
            "run_id": self.result.run_id if self.result else None,
        }


class SubagentManager:
    """Spawns and tracks child agents. Thread-safe.

    Parameters
    ----------
    registry:
        The parent's tool registry — children receive scoped subsets of it.
    gate:
        The parent's permission gate — inherited by every child.
    events_root:
        Root for run logs; each child gets its own ``<run_id>`` directory.
    parent_log:
        Optional :class:`EventLog` of the parent run. ``subagent_spawn`` and
        ``subagent_finish`` events are recorded here.
    agent_kwargs:
        Extra keyword arguments forwarded to every child
        :class:`agentkai.agent.Agent` (e.g. ``max_iterations``,
        ``step_timeout``). ``model``, ``registry``, ``gate`` and
        ``events_root`` are set per child and cannot be overridden here.
    """

    def __init__(self,
                 registry: Registry | None = None,
                 gate: PermissionGate | None = None,
                 events_root: str | Path | None = None,
                 parent_log: EventLog | None = None,
                 **agent_kwargs):
        self.registry = registry
        self.gate = gate or PermissionGate(
            policy={"risk:low": "allow", "risk:medium": "allow",
                    "risk:high": "deny"},
            ask_callback=lambda action: False,
        )
        self.events_root = events_root
        self.parent_log = parent_log
        self.agent_kwargs = dict(agent_kwargs)
        self._children: dict[str, ChildRun] = {}
        self._lock = threading.Lock()

    # -- spawning ------------------------------------------------------

    def spawn(self,
              task: str,
              tools_subset: Sequence[str] | None = None,
              model_alias: str = "claude",
              system_prompt: str = CHILD_SYSTEM_PROMPT) -> str:
        """Spawn a child agent on ``task``. Returns the child id."""
        if not task or not task.strip():
            raise ValueError("subagent task must be a non-empty string")
        parent_registry = self.registry
        if parent_registry is None:
            from .tools import default_registry
            parent_registry = default_registry()
        child_registry = scoped_registry(parent_registry, tools_subset)
        cancel = threading.Event()
        agent = Agent(
            model=model_alias,
            system_prompt=system_prompt,
            registry=child_registry,
            gate=self.gate,  # inherited, never widened
            events_root=self.events_root,
            **self.agent_kwargs,
        )
        child_id = uuid.uuid4().hex[:8]
        child = ChildRun(
            child_id=child_id,
            task=task,
            model_alias=model_alias,
            tool_names=child_registry.names(),
            agent=agent,
            cancel_event=cancel,
        )
        thread = threading.Thread(target=self._run_child, args=(child,),
                                  name=f"subagent-{child_id}", daemon=True)
        child.thread = thread
        with self._lock:
            self._children[child_id] = child
        if self.parent_log is not None:
            self.parent_log.record("subagent_spawn", child_id=child_id,
                                   task=task[:1000], model=model_alias,
                                   tools=child.tool_names)
        thread.start()
        return child_id

    def _run_child(self, child: ChildRun) -> None:
        try:
            child.result = child.agent.run(child.task,
                                           cancel_event=child.cancel_event)
            child.status = child.result.status
        except Exception as exc:  # noqa: BLE001 - child must always report
            child.status = "error"
            child.result = None
            child_error = f"{type(exc).__name__}: {exc}"
        else:
            child_error = child.result.error
        finally:
            child.finished_ts = _utcnow()
            if self.parent_log is not None:
                self.parent_log.record(
                    "subagent_finish", child_id=child.child_id,
                    status=child.status, error=child_error,
                    run_id=child.result.run_id if child.result else None,
                    final_text=(child.result.text[:500]
                                if child.result else ""))

    # -- inspection ----------------------------------------------------

    def get(self, child_id: str) -> ChildRun:
        with self._lock:
            try:
                return self._children[child_id]
            except KeyError:
                raise KeyError(f"unknown subagent {child_id!r}") from None

    def status(self, child_id: str) -> dict:
        """Current status dict for one child (never blocks)."""
        return self.get(child_id).to_dict()

    def list(self) -> list[dict]:
        """Status dicts for all children, oldest first."""
        with self._lock:
            children = sorted(self._children.values(),
                              key=lambda c: c.started_ts)
        return [c.to_dict() for c in children]

    # -- waiting / cancellation -----------------------------------------

    def wait(self, child_id: str, timeout: float | None = None) -> ChildRun:
        """Block until the child finishes. Raises :class:`TimeoutError` on
        timeout, :class:`KeyError` for an unknown child."""
        child = self.get(child_id)
        child.thread.join(timeout)
        if child.thread.is_alive():
            raise TimeoutError(
                f"subagent {child_id} did not finish within {timeout}s")
        return child

    def cancel(self, child_id: str) -> bool:
        """Request cancellation. Returns False if the child already finished."""
        child = self.get(child_id)
        if not child.thread.is_alive():
            return False
        child.cancel_event.set()
        return True

    def wait_all(self, timeout: float | None = None) -> None:
        """Wait for every currently-known child (best effort)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for child_id in [c["child_id"] for c in self.list()]:
            remaining = None if deadline is None else max(
                0.0, deadline - time.monotonic())
            try:
                self.wait(child_id, timeout=remaining)
            except TimeoutError:
                break

    def result_text(self, child_id: str,
                    timeout: float | None = None) -> str:
        """Block for the child, then return its final text (or error)."""
        child = self.wait(child_id, timeout=timeout)
        if child.result is None:
            return f"ERROR: subagent {child_id} ended with status {child.status}"
        return child.result.text

    # -- tools for the parent agent --------------------------------------

    def tools(self) -> list[Tool]:
        """The four subagent tools, bound to this manager."""
        mgr = self

        def _spawn_subagent(task: str, tools: str = "",
                            model: str = "claude") -> dict:
            """Spawn a child agent on a task. Returns its child id."""
            subset = ([t.strip() for t in tools.split(",") if t.strip()]
                      if tools.strip() else None)
            child_id = mgr.spawn(task, tools_subset=subset,
                                 model_alias=model)
            return {"child_id": child_id, "status": "running"}

        def _subagent_status(child_id: str) -> dict:
            """Current status of a child agent."""
            try:
                return mgr.status(child_id)
            except KeyError:
                return f"ERROR: unknown subagent {child_id!r}"

        def _subagent_result(child_id: str, timeout: int = 600) -> dict:
            """Wait for a child agent and return its final text."""
            try:
                child = mgr.wait(child_id,
                                 timeout=max(1, min(int(timeout), 3600)))
            except KeyError:
                return f"ERROR: unknown subagent {child_id!r}"
            except TimeoutError:
                return {"child_id": child_id, "status": "running",
                        "note": f"still running after {timeout}s"}
            text = (child.result.text if child.result
                    else f"ERROR: ended with status {child.status}")
            return {"child_id": child_id, "status": child.status,
                    "text": text}

        def _cancel_subagent(child_id: str) -> dict:
            """Request cancellation of a running child agent."""
            try:
                cancelled = mgr.cancel(child_id)
            except KeyError:
                return f"ERROR: unknown subagent {child_id!r}"
            return {"child_id": child_id,
                    "cancelled": cancelled}

        return [
            Tool(name="spawn_subagent",
                 description=("Spawn a child agent on a self-contained task. "
                              "Returns a child_id; the child runs in the "
                              "background with read-only tools unless you "
                              "name tools explicitly (comma-separated)."),
                 json_schema={"type": "object",
                              "properties": {
                                  "task": {"type": "string"},
                                  "tools": {"type": "string"},
                                  "model": {"type": "string"}},
                              "required": ["task"]},
                 risk="medium", func=_spawn_subagent),
            Tool(name="subagent_status",
                 description="Check a child agent's status without waiting.",
                 json_schema={"type": "object",
                              "properties": {
                                  "child_id": {"type": "string"}},
                              "required": ["child_id"]},
                 risk="low", func=_subagent_status),
            Tool(name="subagent_result",
                 description=("Wait for a child agent (up to `timeout` "
                              "seconds) and return its final result text."),
                 json_schema={"type": "object",
                              "properties": {
                                  "child_id": {"type": "string"},
                                  "timeout": {"type": "integer"}},
                              "required": ["child_id"]},
                 risk="low", func=_subagent_result),
            Tool(name="cancel_subagent",
                 description="Ask a running child agent to stop.",
                 json_schema={"type": "object",
                              "properties": {
                                  "child_id": {"type": "string"}},
                              "required": ["child_id"]},
                 risk="medium", func=_cancel_subagent),
        ]

    def register_tools(self, registry: Registry) -> None:
        """Register the four subagent tools into a parent registry."""
        for tool in self.tools():
            registry.register(tool)


# -- module-level default manager -----------------------------------------

_default_manager: SubagentManager | None = None
_default_lock = threading.Lock()


def default_manager(**kwargs) -> SubagentManager:
    """Process-wide default manager (handy for the CLI and dashboard)."""
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = SubagentManager(**kwargs)
        return _default_manager


def make_manager_factory(parent_registry: Registry,
                         gate: PermissionGate | None = None,
                         events_root: str | Path | None = None,
                         parent_log: EventLog | None = None,
                         **agent_kwargs) -> Callable[[], SubagentManager]:
    """Build a zero-arg factory producing managers bound to a parent run.

    The dashboard/CLI can call the factory per run so each parent run gets
    its own manager (and its own spawn/finish events in its log).
    """
    def _factory() -> SubagentManager:
        return SubagentManager(registry=parent_registry, gate=gate,
                               events_root=events_root, parent_log=parent_log,
                               **agent_kwargs)
    return _factory
