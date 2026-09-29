"""Background scheduler: cron-like recurring jobs + one-shot timers.

Jobs are shell commands or agent prompts stored in SQLite; a lightweight runner
executes due jobs. Delivery rule: stay silent unless the job produced something
worth the user's attention (mirrors the "notify only on signal" pattern).
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Job:
    name: str
    schedule: str  # cron expression, e.g. "*/5 * * * *"
    command: str
    enabled: bool = True


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    schedule TEXT NOT NULL,
    command TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT
);
"""


class Scheduler:
    def __init__(self, db: str | Path = "~/.agentkai/scheduler.db"):
        self.db_path = Path(db).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def add(self, job: Job) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs (name, schedule, command, enabled)"
            " VALUES (?, ?, ?, ?)",
            (job.name, job.schedule, job.command, int(job.enabled)),
        )
        self.conn.commit()

    def remove(self, name: str) -> None:
        self.conn.execute("DELETE FROM jobs WHERE name=?", (name,))
        self.conn.commit()

    def list(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT name, schedule, command, enabled FROM jobs").fetchall()
        return [Job(n, s, c, bool(e)) for n, s, c, e in rows]

    def run_once(self, name: str) -> dict:
        """Run a single job now (used by the runner when its schedule is due)."""
        row = self.conn.execute(
            "SELECT command FROM jobs WHERE name=? AND enabled=1",
            (name,)).fetchone()
        if not row:
            return {"error": f"no enabled job named {name}"}
        p = subprocess.run(row[0], shell=True, capture_output=True, text=True,
                           timeout=600)
        self.conn.execute("UPDATE jobs SET last_run=? WHERE name=?",
                          (time.strftime("%Y-%m-%dT%H:%M:%S"), name))
        self.conn.commit()
        return {"exit_code": p.returncode,
                "output": (p.stdout + p.stderr)[-4000:]}
