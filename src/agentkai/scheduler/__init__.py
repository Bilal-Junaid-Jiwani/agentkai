"""Background scheduler: cron-like recurring jobs + one-shot timers + heartbeat.

Jobs live in SQLite (``~/.agentkai/scheduler.db``) so they survive restarts.
Two kinds of work:

- **legacy shell jobs** (``command`` set): run via subprocess, as in v0.
- **agent jobs** (``prompt`` set): run the Agent loop. This is how cron
  tasks, the heartbeat, and nightly dreaming execute.

Special job types:

- ``heartbeat`` — every N minutes the agent wakes with a checklist prompt
  (from ``~/.agentkai/HEARTBEAT.md`` when present) for proactive checks:
  calendar, messages, failing jobs, anything the user would want to know.
- ``dreaming`` — a nightly job that summarizes the day's runs from the
  event log and consolidates them into the memory daily log.

Delivery rule: jobs stay silent unless they produced something worth the
user's attention (the runner reports; the caller decides what to surface).

Cron syntax is the standard 5-field ``minute hour dom month dow`` with
``*``, ``*/n``, ``a,b``, ``a-b``. One-shot jobs use the schedule
``@at:<ISO datetime>``.
"""
from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HEARTBEAT_PATH = Path("~/.agentkai/HEARTBEAT.md").expanduser()

DEFAULT_HEARTBEAT_CHECKLIST = """\
# Heartbeat checklist
- Review any scheduled jobs that failed since the last heartbeat.
- Check for new messages or events that need the user's attention.
- Look at today's agent runs for anything surprising or worth surfacing.
- Stay silent unless something genuinely needs the user: heartbeat runs are
  routine, and routine means quiet.
"""


@dataclass
class Job:
    name: str
    schedule: str  # cron expression, e.g. "*/5 * * * *" — or "@at:<ISO>" for one-shot
    command: str = ""  # legacy shell command (v0 jobs)
    prompt: str = ""  # agent prompt (new-style jobs)
    model_alias: str = "claude"
    job_type: str = "cron"  # cron | at | heartbeat | dreaming
    enabled: bool = True
    last_run: str = ""  # ISO timestamp of the last execution
    created_ts: str = field(default_factory=lambda: _now().isoformat())

    @property
    def is_oneshot(self) -> bool:
        return self.schedule.startswith("@at:")


SCHEMA_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    schedule TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    model_alias TEXT NOT NULL DEFAULT 'claude',
    job_type TEXT NOT NULL DEFAULT 'cron',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT NOT NULL DEFAULT '',
    created_ts TEXT NOT NULL DEFAULT ''
);
"""

SCHEMA_RUNS = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_ts TEXT NOT NULL,
    finished_ts TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


# -- cron ------------------------------------------------------------------

_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # min hr dom mon dow


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of matching values."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field part in {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"bad cron step in {field!r}")
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"cron value out of range in {field!r}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(schedule: str) -> tuple[set[int], ...]:
    """Parse a 5-field cron expression into five value sets."""
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron schedule needs 5 fields, got {len(fields)}: {schedule!r}")
    return tuple(_parse_cron_field(f, lo, hi)
                 for f, (lo, hi) in zip(fields, _CRON_RANGES))


def cron_matches(schedule: str, dt: datetime) -> bool:
    """True when the cron expression fires at ``dt`` (Vixie dom/dow OR)."""
    minute, hour, dom, month, dow = parse_cron(schedule)
    if dt.minute not in minute or dt.hour not in hour \
            or dt.month not in month:
        return False
    dom_match = dt.day in dom
    # Python: Monday=0..Sunday=6 ; cron: Sunday=0..Saturday=6
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in dow
    dom_star = dom == set(range(1, 32))
    dow_star = dow == set(range(0, 7))
    if dom_star and dow_star:
        day_ok = True
    elif dom_star:
        day_ok = dow_match
    elif dow_star:
        day_ok = dom_match
    else:
        day_ok = dom_match or dow_match
    return day_ok


def cron_next(schedule: str, after: datetime) -> datetime:
    """Next datetime strictly after ``after`` matching the schedule."""
    parse_cron(schedule)  # validate eagerly
    candidate = (after + timedelta(minutes=1)).replace(second=0,
                                                      microsecond=0)
    limit = after + timedelta(days=366)
    while candidate <= limit:
        if cron_matches(schedule, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"no cron occurrence within a year of {after}")


def parse_at(schedule: str) -> datetime:
    """Parse an ``@at:<ISO datetime>`` one-shot schedule."""
    if not schedule.startswith("@at:"):
        raise ValueError(f"not a one-shot schedule: {schedule!r}")
    dt = datetime.fromisoformat(schedule[len("@at:"):])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now().tzinfo)
    return dt


# -- heartbeat / dreaming prompts --------------------------------------------

def build_heartbeat_prompt(heartbeat_path: str | Path = HEARTBEAT_PATH,
                           now: datetime | None = None) -> str:
    """Assemble the heartbeat wake-up prompt.

    Uses ``HEARTBEAT.md`` when it exists, otherwise a built-in checklist.
    """
    now = now or _now()
    path = Path(heartbeat_path).expanduser()
    if path.exists():
        checklist = path.read_text(encoding="utf-8")
        source = f"the user's HEARTBEAT.md checklist ({path})"
    else:
        checklist = DEFAULT_HEARTBEAT_CHECKLIST
        source = ("the default heartbeat checklist (the user can customize "
                  "it by creating ~/.agentkai/HEARTBEAT.md)")
    return (
        f"[heartbeat · {now.isoformat(timespec='minutes')}] Time for a "
        f"proactive check-in. Work through {source}:\n\n{checklist}\n\n"
        "Use your tools to check each item. Report back concisely: only "
        "things the user should actually know about. If everything is "
        "quiet, say so in one line and stop."
    )


def build_dreaming_prompt(date: str | None = None,
                          runs_root: str | Path | None = None,
                          heartbeat_path: str | Path = HEARTBEAT_PATH,
                          memory_dir: str | Path | None = None) -> str:
    """Assemble the nightly dreaming (memory consolidation) prompt.

    ``date`` is ``YYYY-MM-DD`` (defaults to today). The prompt carries
    summaries of the day's runs and instructs the agent to consolidate them
    into the memory daily log.
    """
    from agentkai import events  # local import: keep module import light

    date = date or _now().strftime("%Y-%m-%d")
    summaries: list[dict] = []
    try:
        run_ids = (events.list_runs(root=runs_root) if runs_root
                   else events.list_runs())
        for run_id in run_ids:
            try:
                s = (events.summarize(run_id, root=runs_root) if runs_root
                     else events.summarize(run_id))
            except Exception:
                continue
            summaries.append(s)
    except Exception:
        summaries = []
    mem_dir = Path(memory_dir).expanduser() if memory_dir \
        else Path("~/.agentkai/memory").expanduser()
    daily = mem_dir / f"{date}.md"
    lines = [f"[dreaming · {date}] Nightly memory consolidation.",
             f"{len(summaries)} agent run(s) happened today.",
             "",
             "Run summaries:"]
    for s in summaries:
        lines.append(
            f"- {s['run_id'][:8]} [{s['status']}] model={s['model']} "
            f"tools={','.join(s['tool_calls'][:8]) or 'none'} "
            f"approvals={','.join(s['approvals'][:8]) or 'none'}")
        if s["final_text"]:
            lines.append(f"  final: {s['final_text'][:200]}")
    lines += [
        "",
        f"Decide what is worth remembering and append it to {daily} under a "
        "'## Dreaming' section (create the file if needed). Keep durable "
        "facts and preferences; skip trivia. Use your write_file tool.",
    ]
    return "\n".join(lines)


# -- agent execution -----------------------------------------------------------

def _default_agent_factory(model_alias: str):
    """Build a real Agent for scheduled execution.

    Unattended jobs can never prompt: medium/low risk auto-allows, high
    risk is denied. Imported lazily so the scheduler stays importable
    without the provider stack.
    """
    from .agent import Agent
    from .permissions import PermissionGate
    gate = PermissionGate(
        policy={"risk:low": "allow", "risk:medium": "allow",
                "risk:high": "deny"},
        ask_callback=lambda action: False,
    )
    return Agent(model=model_alias, gate=gate)


# -- scheduler -------------------------------------------------------------------

class Scheduler:
    """SQLite-backed job store + runner.

    Parameters
    ----------
    db:
        Path to the SQLite file (default ``~/.agentkai/scheduler.db``).
    agent_factory:
        ``factory(model_alias) -> agent`` with a ``run(prompt)`` method.
        Defaults to building real agents; tests inject fakes.
    """

    def __init__(self, db: str | Path = "~/.agentkai/scheduler.db",
                 agent_factory: Callable[[str], object] | None = None):
        self.db_path = Path(db).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path),
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(SCHEMA_JOBS)
        self.conn.execute(SCHEMA_RUNS)
        self._migrate()
        self.conn.commit()
        self.agent_factory = agent_factory or _default_agent_factory
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Add newer columns to databases created by the v0 scheduler."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("prompt", "TEXT NOT NULL DEFAULT ''"),
                         ("model_alias", "TEXT NOT NULL DEFAULT 'claude'"),
                         ("job_type", "TEXT NOT NULL DEFAULT 'cron'"),
                         ("created_ts", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    # -- CRUD ------------------------------------------------------------

    def add(self, job: Job) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(name, schedule, command, prompt, model_alias, job_type, "
                " enabled, last_run, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job.name, job.schedule, job.command, job.prompt,
                 job.model_alias, job.job_type, int(job.enabled),
                 job.last_run, job.created_ts),
            )
            self.conn.commit()

    def remove(self, name: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM jobs WHERE name=?", (name,))
            self.conn.commit()
            return cur.rowcount > 0

    def get(self, name: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE name=?",
                                (name,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, enabled_only: bool = False) -> list[Job]:
        q = "SELECT * FROM jobs ORDER BY name"
        if enabled_only:
            q = "SELECT * FROM jobs WHERE enabled=1 ORDER BY name"
        return [self._row_to_job(r) for r in self.conn.execute(q)]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            name=row["name"], schedule=row["schedule"],
            command=row["command"] or "", prompt=row["prompt"] or "",
            model_alias=row["model_alias"] or "claude",
            job_type=row["job_type"] or "cron",
            enabled=bool(row["enabled"]), last_run=row["last_run"] or "",
            created_ts=row["created_ts"] or "")

    # -- legacy shell execution -------------------------------------------

    def run_once(self, name: str) -> dict:
        """Run a single job now via shell (legacy v0 behavior for jobs
        with ``command`` set)."""
        row = self.conn.execute(
            "SELECT command FROM jobs WHERE name=? AND enabled=1",
            (name,)).fetchone()
        if not row or not row["command"]:
            return {"error": f"no enabled shell job named {name}"}
        p = subprocess.run(row["command"], shell=True, capture_output=True,
                           text=True, timeout=600)
        self._stamp(name)
        return {"exit_code": p.returncode,
                "output": (p.stdout + p.stderr)[-4000:]}

    # -- agent execution ---------------------------------------------------

    def run_job(self, name: str) -> dict:
        """Run one agent job now, record the outcome, return a result dict."""
        job = self.get(name)
        if job is None:
            return {"name": name, "status": "error",
                    "summary": f"no job named {name!r}"}
        if not job.enabled:
            return {"name": name, "status": "skipped",
                    "summary": "job disabled"}
        if job.command and not job.prompt:
            # Legacy shell job: keep v0 behavior.
            out = self.run_once(name)
            return {"name": name, "status": "done",
                    "summary": f"shell exit {out.get('exit_code')}",
                    **out}
        prompt = self._prompt_for(job)
        started = _now()
        try:
            agent = self.agent_factory(job.model_alias)
            res = agent.run(prompt)
            status = getattr(res, "status", "done")
            summary = (getattr(res, "text", "") or "")[:500]
            if status not in ("done",):
                summary = f"[{status}] {getattr(res, 'error', '')} {summary}"
        except Exception as exc:  # noqa: BLE001 - jobs report, never raise
            status = "error"
            summary = f"{type(exc).__name__}: {exc}"
        finished = _now()
        self._record_run(name, started, finished, status, summary)
        self._stamp(name, when=finished)
        if job.is_oneshot:
            self._set_enabled(name, False)
        return {"name": name, "status": status, "summary": summary,
                "started_ts": started.isoformat(),
                "finished_ts": finished.isoformat()}

    def _prompt_for(self, job: Job) -> str:
        if job.job_type == "heartbeat":
            return build_heartbeat_prompt(heartbeat_path=HEARTBEAT_PATH)
        if job.job_type == "dreaming":
            return build_dreaming_prompt()
        return job.prompt

    def _stamp(self, name: str, when: datetime | None = None) -> None:
        with self._lock:
            self.conn.execute("UPDATE jobs SET last_run=? WHERE name=?",
                              ((when or _now()).isoformat(), name))
            self.conn.commit()

    def _set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE jobs SET enabled=? WHERE name=?",
                              (int(enabled), name))
            self.conn.commit()

    def _record_run(self, name: str, started: datetime, finished: datetime,
                    status: str, summary: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO job_runs (name, started_ts, finished_ts, "
                "status, summary) VALUES (?, ?, ?, ?, ?)",
                (name, started.isoformat(), finished.isoformat(), status,
                 summary[:2000]))
            self.conn.commit()

    def job_runs(self, name: str | None = None,
                 limit: int = 50) -> list[dict]:
        """Execution history, newest first."""
        if name:
            rows = self.conn.execute(
                "SELECT * FROM job_runs WHERE name=? ORDER BY id DESC "
                "LIMIT ?", (name, limit))
        else:
            rows = self.conn.execute(
                "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- due calculation ----------------------------------------------------

    def due_jobs(self, now: datetime | None = None) -> list[Job]:
        """Enabled jobs whose schedule fires at or before ``now``."""
        now = now or _now()
        due: list[Job] = []
        for job in self.list(enabled_only=True):
            if self._is_due(job, now):
                due.append(job)
        return due

    def _is_due(self, job: Job, now: datetime) -> bool:
        last = (datetime.fromisoformat(job.last_run) if job.last_run
                else None)
        if job.is_oneshot:
            if last is not None:
                return False  # already ran
            return parse_at(job.schedule) <= now
        anchor = last or (datetime.fromisoformat(job.created_ts)
                          if job.created_ts else now - timedelta(seconds=1))
        try:
            nxt = cron_next(job.schedule, anchor)
        except ValueError:
            return False
        return nxt <= now

    def run_due(self, now: datetime | None = None) -> list[dict]:
        """Run every due job. Returns one result dict per job executed."""
        now = now or _now()
        return [self.run_job(job.name) for job in self.due_jobs(now)]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
