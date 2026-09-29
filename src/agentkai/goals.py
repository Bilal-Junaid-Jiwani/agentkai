"""Durable user goals + tracked items (reservations, deliveries, reminders).

Goals are long-lived outcomes the user works toward ("ship the v1 launch",
"learn conversational Urdu"). Tracked items are concrete commitments with
an open/closed lifecycle (a booked flight, a parcel in transit, a reminder).
Both live in SQLite under ``~/.agentkai/goals.db`` — separate from the
markdown memory, because they need structured queries (what's open? what's
stuck?), while the narrative still belongs in MEMORY.md.

Wiring into the scheduler: call :func:`build_goal_briefing_prompt` from a
daily ``dreaming``/``heartbeat`` job (or a dedicated cron job) to get a
prompt that carries the open goals into an agent run. The scheduler module
is deliberately not imported here — pass a store in, get a prompt out.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tools import Tool

DEFAULT_DB = Path("~/.agentkai/goals.db").expanduser()

GOAL_STATUSES = ("active", "paused", "completed", "abandoned")
TRACK_KINDS = ("reservation", "delivery", "reminder", "commitment", "other")
TRACK_STATUSES = ("open", "closed")


def _utcnow() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@dataclass
class Goal:
    id: int
    title: str
    description: str
    status: str
    parent_id: int | None
    created_at: float
    updated_at: float
    subgoals: list["Goal"] = field(default_factory=list)
    activity_count: int = 0


@dataclass
class Activity:
    id: int
    goal_id: int
    ts: float
    kind: str  # progress | setback | note
    text: str


@dataclass
class TrackedItem:
    id: int
    kind: str
    title: str
    status: str
    evidence: str
    created_at: float
    updated_at: float


class GoalError(Exception):
    """Invalid goal/tracking operation."""


class GoalStore:
    """SQLite-backed goals and tracked items."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path).expanduser() if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    parent_id INTEGER REFERENCES goals(id),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id INTEGER NOT NULL REFERENCES goals(id),
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'note',
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS goal_breaks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id INTEGER NOT NULL REFERENCES goals(id),
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tracked_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'other',
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    evidence TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_goals_status
                    ON goals(status);
                CREATE INDEX IF NOT EXISTS idx_tracked_status
                    ON tracked_items(status);
            """)

    # ---- goals --------------------------------------------------------

    def create_goal(self, title: str, description: str = "",
                    parent_id: int | None = None) -> Goal:
        title = title.strip()
        if not title:
            raise GoalError("goal title must not be empty")
        if parent_id is not None and self.get_goal(parent_id) is None:
            raise GoalError(f"no parent goal with id {parent_id}")
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO goals (title, description, status, parent_id, "
                "created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?)",
                (title, description, parent_id, now, now))
            gid = cur.lastrowid
        goal = self.get_goal(gid)
        assert goal is not None
        return goal

    def _row_to_goal(self, row: sqlite3.Row,
                     conn: sqlite3.Connection) -> Goal:
        subs = [self._row_to_goal(r, conn) for r in conn.execute(
            "SELECT * FROM goals WHERE parent_id = ? ORDER BY created_at",
            (row["id"],))]
        act = conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE goal_id = ?",
            (row["id"],)).fetchone()["n"]
        return Goal(id=row["id"], title=row["title"],
                    description=row["description"], status=row["status"],
                    parent_id=row["parent_id"], created_at=row["created_at"],
                    updated_at=row["updated_at"], subgoals=subs,
                    activity_count=act)

    def get_goal(self, goal_id: int) -> Goal | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?",
                               (goal_id,)).fetchone()
            return self._row_to_goal(row, conn) if row else None

    def list_goals(self, status: str | None = None,
                   top_level_only: bool = True) -> list[Goal]:
        with self._connect() as conn:
            if status:
                if status not in GOAL_STATUSES:
                    raise GoalError(f"unknown status {status!r}")
                rows = conn.execute(
                    "SELECT * FROM goals WHERE status = ? ORDER BY updated_at DESC",
                    (status,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM goals ORDER BY updated_at DESC").fetchall()
            goals = [self._row_to_goal(r, conn) for r in rows]
        if top_level_only:
            goals = [g for g in goals if g.parent_id is None]
        return goals

    def update_goal(self, goal_id: int, title: str | None = None,
                    description: str | None = None,
                    status: str | None = None) -> Goal:
        goal = self.get_goal(goal_id)
        if goal is None:
            raise GoalError(f"no goal with id {goal_id}")
        if status is not None and status not in GOAL_STATUSES:
            raise GoalError(f"unknown status {status!r}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE goals SET title = COALESCE(?, title), "
                "description = COALESCE(?, description), "
                "status = COALESCE(?, status), updated_at = ? WHERE id = ?",
                (title, description, status, _utcnow(), goal_id))
        updated = self.get_goal(goal_id)
        assert updated is not None
        return updated

    def close_goal(self, goal_id: int, outcome: str = "completed",
                   note: str = "") -> Goal:
        if outcome not in ("completed", "abandoned"):
            raise GoalError("outcome must be 'completed' or 'abandoned'")
        goal = self.update_goal(goal_id, status=outcome)
        if note:
            self.log_activity(goal_id, "note", f"Closed as {outcome}: {note}")
        return goal

    # ---- activity -----------------------------------------------------

    def log_activity(self, goal_id: int, kind: str, text: str) -> Activity:
        if self.get_goal(goal_id) is None:
            raise GoalError(f"no goal with id {goal_id}")
        if kind not in ("progress", "setback", "note"):
            raise GoalError(f"unknown activity kind {kind!r}")
        text = text.strip()
        if not text:
            raise GoalError("activity text must not be empty")
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO activities (goal_id, ts, kind, text) "
                "VALUES (?, ?, ?, ?)", (goal_id, now, kind, text))
            conn.execute("UPDATE goals SET updated_at = ? WHERE id = ?",
                         (now, goal_id))
            aid = cur.lastrowid
        return Activity(id=aid, goal_id=goal_id, ts=now, kind=kind, text=text)

    def recent_activity(self, goal_id: int, limit: int = 20) -> list[Activity]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activities WHERE goal_id = ? "
                "ORDER BY ts DESC LIMIT ?", (goal_id, limit)).fetchall()
        return [Activity(id=r["id"], goal_id=r["goal_id"], ts=r["ts"],
                         kind=r["kind"], text=r["text"]) for r in rows]

    # ---- breaks (pausing a goal) ----------------------------------------

    def start_break(self, goal_id: int, reason: str = "") -> None:
        if self.get_goal(goal_id) is None:
            raise GoalError(f"no goal with id {goal_id}")
        with self._connect() as conn:
            open_break = conn.execute(
                "SELECT id FROM goal_breaks WHERE goal_id = ? "
                "AND ended_at IS NULL", (goal_id,)).fetchone()
            if open_break:
                raise GoalError("a break is already open for this goal")
            conn.execute(
                "INSERT INTO goal_breaks (goal_id, started_at, reason) "
                "VALUES (?, ?, ?)", (goal_id, _utcnow(), reason))
            conn.execute("UPDATE goals SET status = 'paused', updated_at = ? "
                         "WHERE id = ?", (_utcnow(), goal_id))

    def end_break(self, goal_id: int) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE goal_breaks SET ended_at = ? WHERE goal_id = ? "
                "AND ended_at IS NULL", (_utcnow(), goal_id))
            if cur.rowcount == 0:
                raise GoalError("no open break for this goal")
            conn.execute("UPDATE goals SET status = 'active', updated_at = ? "
                         "WHERE id = ?", (_utcnow(), goal_id))

    # ---- tracked items --------------------------------------------------

    def track_open(self, title: str, kind: str = "other") -> TrackedItem:
        title = title.strip()
        if not title:
            raise GoalError("tracked item title must not be empty")
        if kind not in TRACK_KINDS:
            raise GoalError(f"unknown kind {kind!r}")
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tracked_items (kind, title, status, created_at, "
                "updated_at) VALUES (?, ?, 'open', ?, ?)",
                (kind, title, now, now))
            tid = cur.lastrowid
        item = self.get_tracked(tid)
        assert item is not None
        return item

    def get_tracked(self, item_id: int) -> TrackedItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tracked_items WHERE id = ?",
                               (item_id,)).fetchone()
        return self._row_to_tracked(row) if row else None

    @staticmethod
    def _row_to_tracked(row: sqlite3.Row) -> TrackedItem:
        return TrackedItem(id=row["id"], kind=row["kind"], title=row["title"],
                           status=row["status"], evidence=row["evidence"],
                           created_at=row["created_at"],
                           updated_at=row["updated_at"])

    def list_tracked(self, status: str | None = None) -> list[TrackedItem]:
        with self._connect() as conn:
            if status:
                if status not in TRACK_STATUSES:
                    raise GoalError(f"unknown status {status!r}")
                rows = conn.execute(
                    "SELECT * FROM tracked_items WHERE status = ? "
                    "ORDER BY updated_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tracked_items ORDER BY updated_at DESC"
                ).fetchall()
        return [self._row_to_tracked(r) for r in rows]

    def track_close(self, item_id: int, evidence: str = "") -> TrackedItem:
        if self.get_tracked(item_id) is None:
            raise GoalError(f"no tracked item with id {item_id}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE tracked_items SET status = 'closed', evidence = ?, "
                "updated_at = ? WHERE id = ?",
                (evidence, _utcnow(), item_id))
        item = self.get_tracked(item_id)
        assert item is not None
        return item


# ---- daily briefing prompt --------------------------------------------------

def build_goal_briefing_prompt(store: GoalStore | None = None,
                               now: datetime | None = None) -> str:
    """Assemble a daily goal-briefing prompt for a scheduler job.

    Wire it like the scheduler's dreaming/heartbeat prompts: create a cron
    job whose agent prompt is the return value of this function, e.g.
    run once a morning. The agent reviews open goals and open tracked
    items, nudges on stale ones, and logs the briefing as activity.
    """
    store = store or GoalStore()
    now = now or datetime.now()
    active = store.list_goals(status="active")
    open_items = store.list_tracked(status="open")

    lines = [f"[goal-briefing · {now.strftime('%Y-%m-%d %H:%M')}] "
             f"Daily goal check-in. {len(active)} active goal(s), "
             f"{len(open_items)} open tracked item(s).", ""]
    if active:
        lines.append("Active goals:")
        for g in active:
            recent = store.recent_activity(g.id, limit=1)
            last = (f"last activity {_iso(recent[0].ts)}: "
                    f"{recent[0].text[:120]}" if recent
                    else "no activity logged yet")
            lines.append(f"- #{g.id} {g.title} [{g.status}] — {last}")
            for s in g.subgoals:
                lines.append(f"    - #{s.id} {s.title} [{s.status}]")
        lines.append("")
    if open_items:
        lines.append("Open tracked items:")
        for t in open_items:
            lines.append(f"- #{t.id} [{t.kind}] {t.title} "
                         f"(open since {_iso(t.created_at)})")
        lines.append("")
    lines += [
        "For each goal: note one concrete next step the user could take "
        "today. Flag anything that has been idle for over a week. For each "
        "open tracked item: check whether it still needs attention or can "
        "be closed with evidence. Log this briefing with goals_log_activity "
        "on the goals you reviewed, then give the user a short, casual "
        "summary — only what they'd actually want to hear.",
    ]
    return "\n".join(lines)


# ---- agent tools ------------------------------------------------------------

def goal_tools(store: GoalStore | None = None) -> list[Tool]:
    """Tools the agent uses to manage the user's goals and tracked items."""
    st = store or GoalStore()

    def _goals_create(title: str, description: str = "",
                      parent_id: int = 0) -> dict:
        try:
            g = st.create_goal(title, description,
                               parent_id or None)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"ok": True, "id": g.id, "title": g.title}

    def _goals_list(status: str = "active") -> dict:
        try:
            goals = st.list_goals(status=status or None)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"goals": [
            {"id": g.id, "title": g.title, "status": g.status,
             "subgoals": [{"id": s.id, "title": s.title,
                           "status": s.status} for s in g.subgoals],
             "activity_count": g.activity_count} for g in goals]}

    def _goals_log_activity(goal_id: int, kind: str, text: str) -> dict:
        try:
            a = st.log_activity(goal_id, kind, text)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"ok": True, "activity_id": a.id}

    def _goals_close(goal_id: int, outcome: str = "completed",
                     note: str = "") -> dict:
        try:
            g = st.close_goal(goal_id, outcome, note)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"ok": True, "id": g.id, "status": g.status}

    def _track_open(title: str, kind: str = "other") -> dict:
        try:
            t = st.track_open(title, kind)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"ok": True, "id": t.id, "title": t.title, "kind": t.kind}

    def _track_close(item_id: int, evidence: str = "") -> dict:
        try:
            t = st.track_close(item_id, evidence)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"ok": True, "id": t.id, "status": t.status}

    def _track_list(status: str = "open") -> dict:
        try:
            items = st.list_tracked(status=status or None)
        except GoalError as exc:
            return f"ERROR: {exc}"
        return {"items": [
            {"id": t.id, "kind": t.kind, "title": t.title,
             "status": t.status, "evidence": t.evidence} for t in items]}

    return [
        Tool(name="goals_create",
             description="Create a durable user goal (e.g. 'ship v1 launch').",
             json_schema={"type": "object",
                          "properties": {
                              "title": {"type": "string"},
                              "description": {"type": "string"},
                              "parent_id": {"type": "integer",
                                             "description": "make this a subgoal of an existing goal"},
                          },
                          "required": ["title"]},
             risk="medium", func=_goals_create),
        Tool(name="goals_list",
             description="List the user's goals, optionally filtered by status.",
             json_schema={"type": "object",
                          "properties": {
                              "status": {"type": "string",
                                         "enum": list(GOAL_STATUSES) + [""]},
                          }},
             risk="low", func=_goals_list),
        Tool(name="goals_log_activity",
             description="Log progress, a setback, or a note on a goal.",
             json_schema={"type": "object",
                          "properties": {
                              "goal_id": {"type": "integer"},
                              "kind": {"type": "string",
                                       "enum": ["progress", "setback", "note"]},
                              "text": {"type": "string"},
                          },
                          "required": ["goal_id", "kind", "text"]},
             risk="medium", func=_goals_log_activity),
        Tool(name="goals_close",
             description="Close a goal as completed or abandoned, with an optional note.",
             json_schema={"type": "object",
                          "properties": {
                              "goal_id": {"type": "integer"},
                              "outcome": {"type": "string",
                                          "enum": ["completed", "abandoned"]},
                              "note": {"type": "string"},
                          },
                          "required": ["goal_id"]},
             risk="medium", func=_goals_close),
        Tool(name="track_open",
             description="Open a tracked item: reservation, delivery, reminder, or commitment.",
             json_schema={"type": "object",
                          "properties": {
                              "title": {"type": "string"},
                              "kind": {"type": "string",
                                       "enum": list(TRACK_KINDS)},
                          },
                          "required": ["title"]},
             risk="medium", func=_track_open),
        Tool(name="track_close",
             description="Close a tracked item with evidence of the outcome.",
             json_schema={"type": "object",
                          "properties": {
                              "item_id": {"type": "integer"},
                              "evidence": {"type": "string"},
                          },
                          "required": ["item_id"]},
             risk="medium", func=_track_close),
        Tool(name="track_list",
             description="List tracked items, optionally filtered by status.",
             json_schema={"type": "object",
                          "properties": {
                              "status": {"type": "string",
                                         "enum": list(TRACK_STATUSES) + [""]},
                          }},
             risk="low", func=_track_list),
    ]
