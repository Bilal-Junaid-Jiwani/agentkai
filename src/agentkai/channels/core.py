"""Messaging gateway core: routing, sessions, approvals.

A long-running :class:`Gateway` connects chat channels (Telegram, Discord,
WebChat, WhatsApp bridge) and routes inbound messages to agent sessions:

- one isolated session per (channel, peer); identity linking can merge a
  person's identities across channels into one session (see channels.yaml)
- per-channel allowlists: unknown senders get no reply (logged)
- every inbound/outbound message is logged to the session's event log
- destructive/medium-risk tools go through the permission gate; the
  gate's ask-callback messages the owner and waits for APPROVE/DENY
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..events import EventLog
from ..permissions import Action, PermissionGate

DEFAULT_DB = Path("~/.agentkai/sessions.db").expanduser()
DEFAULT_CONFIG = Path("~/.agentkai/channels.yaml").expanduser()

APPROVE_RE = re.compile(r"^\s*(approve|deny)\s+([0-9a-f]{4,16})\s*$",
                        re.IGNORECASE)


# ---- messages ---------------------------------------------------------------

@dataclass
class InboundMessage:
    """One normalized inbound chat message."""
    channel: str          # "telegram" | "discord" | "webchat" | "whatsapp"
    peer_id: str          # channel-native sender id (chat id, user id, ...)
    peer_name: str | None
    text: str
    message_id: str | None = None
    timestamp: float = 0.0

    def sender_key(self) -> str:
        return f"{self.channel}:{self.peer_id}"


class Channel(ABC):
    """One chat transport. Network I/O lives behind small methods so tests
    can subclass with fake transports."""

    name: str = "channel"

    def __init__(self) -> None:
        self.on_message: Callable[[InboundMessage], None] | None = None

    @abstractmethod
    def start(self, on_message: Callable[[InboundMessage], None]) -> None:
        """Begin receiving; call on_message for each inbound message."""

    @abstractmethod
    def stop(self) -> None:
        """Stop receiving and release resources."""

    @abstractmethod
    def send_text(self, peer_id: str, text: str) -> None:
        """Deliver one text message (already split to the channel limit)."""


# ---- message splitting -------------------------------------------------------

def split_text(text: str, limit: int = 4000) -> list[str]:
    """Split *text* into chunks of at most *limit* chars.

    Prefers paragraph breaks, then line breaks, then word boundaries; only
    splits mid-word when a single word exceeds the limit.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > limit // 4:  # avoid degenerate tiny chunks
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit  # one huge word: hard split
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]


# ---- session store ------------------------------------------------------------

class SessionStore:
    """SQLite-backed session registry + message history.

    ``~/.agentkai/sessions.db`` holds:
      sessions — session_key (PK), channel, peer_id, linked_id, timestamps
      messages — per-session chat history (for context restore on restart)
    """

    def __init__(self, path: str | Path = DEFAULT_DB,
                 history_cap: int = 200) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.history_cap = history_cap
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(
                session_key TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                linked_id TEXT,
                created_at REAL NOT NULL,
                last_active REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                direction TEXT NOT NULL,   -- 'in' | 'out'
                text TEXT NOT NULL,
                ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_key, id);
        """)
        self._db.commit()

    def get_or_create(self, session_key: str, channel: str,
                      peer_id: str, linked_id: str | None = None) -> dict:
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT session_key, channel, peer_id, linked_id, "
                "created_at, last_active FROM sessions WHERE session_key=?",
                (session_key,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                    (session_key, channel, peer_id, linked_id, now, now))
                self._db.commit()
                return {"session_key": session_key, "channel": channel,
                        "peer_id": peer_id, "linked_id": linked_id,
                        "created_at": now, "last_active": now}
            self._db.execute(
                "UPDATE sessions SET last_active=? WHERE session_key=?",
                (now, session_key))
            self._db.commit()
            keys = ("session_key", "channel", "peer_id", "linked_id",
                    "created_at", "last_active")
            return dict(zip(keys, row))

    def append_message(self, session_key: str, direction: str,
                       text: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages(session_key, direction, text, ts) "
                "VALUES (?,?,?,?)",
                (session_key, direction, text, time.time()))
            # Cap history per session (cheap: delete the oldest overflow).
            self._db.execute(
                """DELETE FROM messages WHERE session_key=? AND id NOT IN (
                       SELECT id FROM messages WHERE session_key=?
                       ORDER BY id DESC LIMIT ?)""",
                (session_key, session_key, self.history_cap))
            self._db.commit()

    def recent(self, session_key: str, limit: int = 20) -> list[tuple[str, str]]:
        """Newest-last (direction, text) history for context restore."""
        with self._lock:
            rows = self._db.execute(
                "SELECT direction, text FROM messages WHERE session_key=? "
                "ORDER BY id DESC LIMIT ?",
                (session_key, limit)).fetchall()
        return [(d, t) for d, t in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ---- config -------------------------------------------------------------------

def _default_config() -> dict:
    return {
        "owner": "",  # e.g. "telegram:123456" — receives approval requests
        "approval_timeout": 300,
        "model": "claude",
        "auto_approve": False,
        "identity_links": {},  # linked_id -> ["telegram:1", "discord:2", ...]
        "channels": {
            "telegram": {"enabled": False, "token_env": "TELEGRAM_BOT_TOKEN",
                         "allowlist": [], "owner_peer": ""},
            "discord": {"enabled": False, "token_env": "DISCORD_BOT_TOKEN",
                        "allowlist": [], "owner_peer": ""},
            "webchat": {"enabled": True, "port": 18790,
                        "allowlist": ["*"], "owner_peer": "local"},
            "whatsapp": {"enabled": False,
                         "sidecar_url": "http://localhost:18791",
                         "allowlist": [], "owner_peer": ""},
        },
    }


def load_config(path: str | Path | None = None) -> dict:
    """Load ~/.agentkai/channels.yaml, filling defaults. Never logs secrets."""
    import yaml  # pyyaml is a core dependency
    cfg = _default_config()
    path = Path(path).expanduser() if path else DEFAULT_CONFIG
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        for key, val in user.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                for sub, subval in val.items():
                    if isinstance(subval, dict) and isinstance(merged.get(sub), dict):
                        merged[sub] = {**merged[sub], **subval}
                    else:
                        merged[sub] = subval
                cfg[key] = merged
            else:
                cfg[key] = val
    return cfg


def write_example_config(path: str | Path | None = None) -> Path:
    """Write an annotated example config (no secrets) if none exists."""
    import yaml
    path = Path(path).expanduser() if path else DEFAULT_CONFIG
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    example = _default_config()
    example["owner"] = "telegram:YOUR_CHAT_ID"
    example["channels"]["telegram"]["allowlist"] = ["YOUR_CHAT_ID"]
    example["channels"]["telegram"]["owner_peer"] = "YOUR_CHAT_ID"
    example["identity_links"] = {
        "owner": ["telegram:YOUR_CHAT_ID", "discord:YOUR_USER_ID"]
    }
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# agentkai messaging gateway config — see channels/README.md\n")
        yaml.safe_dump(example, fh, sort_keys=False)
    return path


# ---- gateway --------------------------------------------------------------------

@dataclass
class _PendingApproval:
    approval_id: str
    session_key: str
    action: Action
    event: threading.Event
    decision: bool | None = None


class Gateway:
    """Routes channel messages to per-session agent runs.

    Args:
        config: channels.yaml dict (see :func:`load_config`).
        channels: name -> Channel instances to manage.
        agent_factory: ``(session_key) -> agent``; the default builds a real
            :class:`agentkai.agent.Agent` with a chat-approval gate.
        store: SessionStore (defaults to ~/.agentkai/sessions.db).
        max_workers: cap on concurrent agent runs.
    """

    def __init__(self, config: dict, channels: dict[str, Channel],
                 agent_factory: Callable[[str], object] | None = None,
                 store: SessionStore | None = None,
                 max_workers: int = 4) -> None:
        self.config = config
        self.channels = channels
        self.store = store or SessionStore()
        self.agent_factory = agent_factory or self._default_agent_factory
        self.approval_timeout = float(config.get("approval_timeout", 300))
        self.owner = config.get("owner") or ""
        # identity_links: linked_id -> [sender_key, ...]
        self._link_of: dict[str, str] = {}
        for linked_id, keys in (config.get("identity_links") or {}).items():
            for k in keys:
                self._link_of[k] = linked_id
        self._agents: dict[str, object] = {}
        self._agents_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._approvals: dict[str, _PendingApproval] = {}
        self._approvals_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                           thread_name_prefix="agentkai-run")
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._running = True
        for name, ch in self.channels.items():
            ch.start(self._on_message)

    def stop(self) -> None:
        self._running = False
        for ch in self.channels.values():
            try:
                ch.stop()
            except Exception:
                pass
        self._executor.shutdown(wait=False)

    # -- session identity ---------------------------------------------------

    def session_key_for(self, msg: InboundMessage) -> str:
        sender = msg.sender_key()
        linked = self._link_of.get(sender)
        if linked:
            return f"linked:{linked}"
        return sender

    def _is_allowed(self, msg: InboundMessage) -> bool:
        chan_cfg = (self.config.get("channels") or {}).get(msg.channel, {})
        allowlist = chan_cfg.get("allowlist") or []
        if "*" in allowlist:
            return True
        if msg.peer_id in allowlist:
            return True
        owner_peer = chan_cfg.get("owner_peer") or ""
        if owner_peer and msg.peer_id == owner_peer:
            return True
        return False

    def _is_owner(self, msg: InboundMessage) -> bool:
        if self.owner and msg.sender_key() == self.owner:
            return True
        chan_cfg = (self.config.get("channels") or {}).get(msg.channel, {})
        return bool(chan_cfg.get("owner_peer")) and \
            msg.peer_id == chan_cfg["owner_peer"]

    # -- inbound ------------------------------------------------------------

    def _on_message(self, msg: InboundMessage) -> None:
        """Entry point for every channel callback (runs in channel threads)."""
        if not self._running:
            return
        # Approval replies from the owner take priority over agent runs.
        if self._is_owner(msg):
            m = APPROVE_RE.match(msg.text or "")
            if m:
                self._resolve_approval(m.group(2).lower(),
                                       m.group(1).lower() == "approve",
                                       via=msg)
                return
        if not self._is_allowed(msg):
            self._log_security(msg)
            return
        session_key = self.session_key_for(msg)
        self.store.get_or_create(session_key, msg.channel, msg.peer_id,
                                 linked_id=self._link_of.get(msg.sender_key()))
        self.store.append_message(session_key, "in", msg.text)
        self._session_event(session_key, "channel_in", channel=msg.channel,
                            peer_id=msg.peer_id, peer_name=msg.peer_name,
                            text=msg.text[:2000])
        lock = self._session_lock(session_key)
        if not lock.acquire(blocking=False):
            self._reply(msg.channel, msg.peer_id,
                        "Still working on your previous message — one moment…")
            return
        self._executor.submit(self._run_session, session_key, msg, lock)

    def _session_lock(self, session_key: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(session_key,
                                                  threading.Lock())

    def _log_security(self, msg: InboundMessage) -> None:
        # Unknown senders: logged, never replied to.
        self._session_event("security", "rejected_sender", channel=msg.channel,
                            peer_id=msg.peer_id, peer_name=msg.peer_name,
                            text=(msg.text or "")[:200])

    # -- agent runs ----------------------------------------------------------

    def _default_agent_factory(self, session_key: str):
        from ..agent import Agent
        from ..permissions import PermissionGate
        gate = PermissionGate(ask_callback=self._ask_via_owner)
        agent = Agent(model=self.config.get("model", "claude"), gate=gate)
        # Restore recent history so a gateway restart keeps context.
        history = self.store.recent(session_key, limit=20)
        for direction, text in history[:-1]:  # last msg is the current prompt
            role = "user" if direction == "in" else "assistant"
            agent.messages.append({"role": role, "content": text})
        return agent

    def _run_session(self, session_key: str, msg: InboundMessage,
                     lock: threading.Lock) -> None:
        try:
            with self._agents_lock:
                agent = self._agents.get(session_key)
                if agent is None:
                    agent = self.agent_factory(session_key)
                    self._agents[session_key] = agent
            result = agent.run(msg.text)
            reply = (result.text or "").strip() or "(no reply)"
            self._send_reply(session_key, msg.channel, msg.peer_id, reply)
        except Exception as exc:  # noqa: BLE001 - chat must never die silently
            self._session_event(session_key, "run_error",
                                error=f"{type(exc).__name__}: {exc}")
            self._reply(msg.channel, msg.peer_id,
                        f"Sorry — something went wrong: {type(exc).__name__}")
        finally:
            lock.release()

    def _send_reply(self, session_key: str, channel: str, peer_id: str,
                    text: str) -> None:
        limit = {"telegram": 4096, "discord": 2000}.get(channel, 4000)
        for chunk in split_text(text, limit=limit):
            self._reply(channel, peer_id, chunk)
        self.store.append_message(session_key, "out", text)
        self._session_event(session_key, "channel_out", channel=channel,
                            peer_id=peer_id, text=text[:2000])

    def _reply(self, channel: str, peer_id: str, text: str) -> None:
        ch = self.channels.get(channel)
        if ch is not None:
            try:
                ch.send_text(peer_id, text)
            except Exception:
                pass  # a failed send must not kill the run

    # -- owner approvals -------------------------------------------------------

    def _ask_via_owner(self, action: Action) -> bool:
        """PermissionGate ask-callback: message the owner, wait for reply."""
        approval_id = secrets.token_hex(4)
        wait = _PendingApproval(approval_id=approval_id, session_key="",
                                action=action, event=threading.Event())
        with self._approvals_lock:
            self._approvals[approval_id] = wait
        owner_channel, _, owner_peer = self.owner.partition(":")
        sent = False
        if owner_channel and owner_peer and owner_channel in self.channels:
            self._reply(
                owner_channel, owner_peer,
                f"Approval needed [{approval_id}]:\n{action.describe()}\n"
                f"Reply `APPROVE {approval_id}` or `DENY {approval_id}`.")
            sent = True
        if not sent:
            with self._approvals_lock:
                self._approvals.pop(approval_id, None)
            return False
        decided = wait.event.wait(timeout=self.approval_timeout)
        with self._approvals_lock:
            self._approvals.pop(approval_id, None)
        return bool(decided and wait.decision)

    def _resolve_approval(self, approval_id: str, approved: bool,
                          via: InboundMessage) -> None:
        with self._approvals_lock:
            wait = self._approvals.get(approval_id)
        if wait is None:
            self._reply(via.channel, via.peer_id,
                        f"No pending approval {approval_id}.")
            return
        wait.decision = approved
        wait.event.set()
        self._reply(via.channel, via.peer_id,
                    f"{'Approved' if approved else 'Denied'} {approval_id}.")
        self._session_event("security", "approval_resolved",
                            approval_id=approval_id, approved=approved,
                            via=via.sender_key())

    # -- session event log -------------------------------------------------------

    def _session_event(self, session_key: str, type: str, **data) -> None:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_key)[:64]
        try:
            log = EventLog(run_id=f"gateway-{safe or 'unknown'}")
            log.record(type, **data)
            log.close()
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------

    def pending_approvals(self) -> list[dict]:
        with self._approvals_lock:
            return [{"id": p.approval_id,
                     "action": p.action.describe()} for p in
                    self._approvals.values()]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
