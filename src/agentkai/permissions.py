"""Risk-based permission gate: allow / ask / deny for every tool call.

Tool authors declare a risk level on the tool (``agentkai.tools.Tool.risk``):

    low     — reading files, listings                      -> allow
    medium  — writing files, network fetches                -> ask
    high    — shell execution and other powerful actions    -> ask

The gate is consulted before every tool execution and every decision is
written to an audit log (in memory, plus optional JSONL file).

Interop with ``agentkai.tools``: ``Tool.run`` accepts a gate callable with
the signature ``gate(action: dict) -> "allow" | "ask" | "deny"`` where
``action == {"tool": name, "args": {...}, "risk": risk}``. Use
:meth:`PermissionGate.as_gate_fn` to get such a callable. Note that
``Tool.run`` *raises* on "ask" (``ApprovalRequired``) instead of prompting —
the agent loop is expected to call :meth:`PermissionGate.check` first (which
does prompt via the ask hook) and only run the tool on "allow".

This module is dependency-free on purpose: tools/, the agent loop, and the
dashboard all import it.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

Decision = Literal["allow", "ask", "deny"]
Risk = Literal["low", "medium", "high"]

DEFAULT_RISK_POLICY: dict[str, Decision] = {
    "low": "allow",
    "medium": "ask",
    "high": "ask",
}


@dataclass
class Action:
    """One proposed tool invocation awaiting a permission decision."""
    tool: str
    args: dict = field(default_factory=dict)
    risk: str = "medium"

    @staticmethod
    def from_dict(d: dict) -> "Action":
        """Build from the ``{"tool", "args", "risk"}`` dict shape that
        ``agentkai.tools.Tool.run`` passes to gate callables."""
        return Action(tool=d.get("tool", "?"),
                      args=dict(d.get("args") or {}),
                      risk=d.get("risk", "medium"))

    def to_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "risk": self.risk}

    def describe(self) -> str:
        arg_bits = ", ".join(f"{k}={_short(v)}" for k, v in self.args.items())
        return f"{self.tool}({arg_bits}) [risk={self.risk}]"


def _short(v, n: int = 60) -> str:
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def cli_ask(action: Action) -> bool:
    """Default ask hook for terminal use. Returns True when approved."""
    print(f"\nagentkai wants to run: {action.describe()}")
    try:
        answer = input("Allow once? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


class PermissionGate:
    """Decides allow/ask/deny for tool actions and audits every decision.

    Parameters
    ----------
    policy:
        Optional overrides. Keys are ``"risk:<level>"`` (e.g. ``"risk:high"``)
        or ``"tool:<name>"`` (e.g. ``"tool:exec"``); values are
        ``"allow"``/``"ask"``/``"deny"``. Tool-specific entries win over
        risk-level entries, which win over the built-in defaults. Set
        ``"risk:high": "deny"`` (or ``"tool:exec": "deny"``) for a
        non-interactive lockdown.
    ask_callback:
        Called as ``ask_callback(action) -> bool`` when the decision is
        ``"ask"``. Defaults to :func:`cli_ask`. The dashboard will inject its
        own approval-card hook here.
    audit_path:
        Optional JSONL file every decision is appended to.
    """

    def __init__(self,
                 policy: dict[str, Decision] | None = None,
                 ask_callback: Callable[[Action], bool] | None = None,
                 audit_path: str | Path | None = None):
        self.policy = dict(policy or {})
        self.ask_callback = ask_callback or cli_ask
        self.audit_path = Path(audit_path).expanduser() if audit_path else None
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit: list[dict] = []
        self._lock = threading.Lock()

    # -- decision ------------------------------------------------------

    def decide(self, action: Action) -> Decision:
        """Policy lookup only — no prompting, no side effects."""
        tool_key = f"tool:{action.tool}"
        if tool_key in self.policy:
            return self.policy[tool_key]
        risk_key = f"risk:{action.risk}"
        if risk_key in self.policy:
            return self.policy[risk_key]
        return DEFAULT_RISK_POLICY.get(action.risk, "ask")

    def check(self, action: Action) -> Decision:
        """Full gate: decide, prompt on "ask", audit, and return the final
        decision ("allow" or "deny")."""
        decision = self.decide(action)
        approved: bool | None = None
        if decision == "ask":
            try:
                approved = bool(self.ask_callback(action))
            except Exception:
                approved = False
            decision = "allow" if approved else "deny"
        self._record(action, decision, approved)
        return decision

    def as_gate_fn(self) -> Callable[[dict], str]:
        """Adapter for ``agentkai.tools.Tool.run(_gate=...)``.

        Returns "allow"/"ask"/"deny" for the dict-style action. Note
        ``Tool.run`` raises on "ask"/"deny" instead of prompting, so prefer
        :meth:`check` in the agent loop and use this adapter for other
        callers that handle the exceptions themselves.
        """
        def gate(action_dict: dict) -> str:
            return self.decide(Action.from_dict(action_dict))
        return gate

    # -- audit ----------------------------------------------------------

    def _record(self, action: Action, decision: Decision,
                approved: bool | None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": action.tool,
            "args": action.args,
            "risk": action.risk,
            "decision": decision,
            "approved": approved,
        }
        with self._lock:
            self._audit.append(entry)
            if self.audit_path:
                with open(self.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")

    def audit_log(self) -> list[dict]:
        """Every decision this gate has made, oldest first."""
        with self._lock:
            return list(self._audit)

    def approvals_for(self, tool: str) -> list[dict]:
        return [e for e in self.audit_log() if e["tool"] == tool]
