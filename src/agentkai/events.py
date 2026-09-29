"""Append-only event log: the durable substrate of every agent run.

Each run writes JSONL events to ``~/.agentkai/runs/<run_id>/events.jsonl``.
The log is the single source of truth — replay, dashboard views, and
debugging are all projections over it.

Event types:
    run_start   — run begins (model, prompt, config snapshot)
    llm_message — one assistant message (text + normalized tool calls)
    tool_call   — a tool invocation (name, args)
    tool_result — a tool's output (truncated for storage)
    approval    — permission-gate decision for a tool call
    run_end     — run finished (status, final text)
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_ROOT = Path("~/.agentkai/runs").expanduser()

# How much of a tool result we keep in the event log (full output stays in
# the live context only; the log keeps enough for debugging).
MAX_RESULT_CHARS = 4000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    seq: int
    ts: str
    type: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type,
                "data": self.data}

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(seq=d["seq"], ts=d["ts"], type=d["type"],
                     data=d.get("data", {}))


class EventLog:
    """Thread-safe append-only JSONL writer for one run."""

    def __init__(self, run_id: str | None = None,
                 root: str | Path | None = None,
                 on_event: Callable[[Event], None] | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.root = Path(root).expanduser() if root else DEFAULT_ROOT
        self.dir = self.root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        if on_event is not None:
            self._listeners.append(on_event)
        # Resume the sequence when reopening an existing run's log, so
        # seqs stay unique and replay order is stable across sessions.
        self._seq = 0
        if self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._seq = max(self._seq,
                                        int(json.loads(line).get("seq", 0)))
            except Exception:
                pass
        # Open in append mode once; flush after every record.
        self._fh = open(self.path, "a", encoding="utf-8")

    def record(self, type: str, **data) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(seq=self._seq, ts=_utcnow(), type=type, data=data)
            self._fh.write(json.dumps(ev.to_dict(), default=str) + "\n")
            self._fh.flush()
            listeners = list(self._listeners)
        # Notify outside the lock: a listener must never block recording,
        # and a listener bug must never break the agent loop.
        for fn in listeners:
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        """Attach a live event listener (called from the recording thread)."""
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Event], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def record_tool_result(self, call_id: str, name: str, output: str,
                           ok: bool = True) -> Event:
        """Record a tool result, truncating long outputs for storage."""
        stored = output
        truncated = False
        if len(output) > MAX_RESULT_CHARS:
            stored = (output[:MAX_RESULT_CHARS]
                      + f"\n... [truncated {len(output) - MAX_RESULT_CHARS} chars]")
            truncated = True
        return self.record("tool_result", call_id=call_id, name=name,
                           output=stored, ok=ok, truncated=truncated)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def replay(run_id: str, root: str | Path = DEFAULT_ROOT) -> Iterator[Event]:
    """Yield every event of a finished (or live) run, in order."""
    path = Path(root).expanduser() / run_id / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no event log for run {run_id!r} at {path}")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Event.from_dict(json.loads(line))


def list_runs(root: str | Path = DEFAULT_ROOT) -> list[str]:
    """Newest-first run ids that have an event log."""
    root = Path(root).expanduser()
    if not root.exists():
        return []
    runs = [p.name for p in root.iterdir()
            if p.is_dir() and (p / "events.jsonl").exists()]
    runs.sort(key=lambda r: (root / r / "events.jsonl").stat().st_mtime,
              reverse=True)
    return runs


def summarize(run_id: str, root: str | Path = DEFAULT_ROOT) -> dict:
    """Small projection over a run: status, model, tool calls, approvals."""
    tool_calls: list[str] = []
    approvals: list[str] = []
    status = "unknown"
    model = ""
    final_text = ""
    for ev in replay(run_id, root):
        d = ev.data
        if ev.type == "run_start":
            model = d.get("model", "")
        elif ev.type == "tool_call":
            tool_calls.append(d.get("name", "?"))
        elif ev.type == "approval":
            approvals.append(f"{d.get('tool')}:{d.get('decision')}")
        elif ev.type == "run_end":
            status = d.get("status", "unknown")
            final_text = (d.get("final_text") or "")[:500]
    return {"run_id": run_id, "model": model, "status": status,
            "tool_calls": tool_calls, "approvals": approvals,
            "final_text": final_text}
