"""Tests for the scheduler: cron math, persistence, agent jobs, heartbeat,
dreaming, and one-shot jobs. No network, no real agents — a fake factory.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import typer.testing

from agentkai import cli as cli_mod
from agentkai.events import EventLog
from agentkai.scheduler import (
    HEARTBEAT_PATH,
    Job,
    Scheduler,
    build_dreaming_prompt,
    build_heartbeat_prompt,
    cron_matches,
    cron_next,
    parse_at,
    parse_cron,
)


def dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.astimezone()


# -- cron parsing ------------------------------------------------------------

def test_parse_cron_star_and_step():
    minute, hour, dom, month, dow = parse_cron("*/15 * * * *")
    assert minute == {0, 15, 30, 45}
    assert hour == set(range(24))


def test_parse_cron_lists_ranges():
    _, _, _, _, dow = parse_cron("0 9 * * 1-5")
    assert dow == {1, 2, 3, 4, 5}
    minute, *_ = parse_cron("0,30 8 * * *")
    assert minute == {0, 30}


def test_parse_cron_bad_field_count():
    with pytest.raises(ValueError):
        parse_cron("*/5 * * *")


def test_parse_cron_out_of_range():
    with pytest.raises(ValueError):
        parse_cron("61 * * * *")


def test_cron_matches_basic():
    assert cron_matches("* * * * *", dt("2026-09-29T10:07"))
    assert cron_matches("*/5 * * * *", dt("2026-09-29T10:05"))
    assert not cron_matches("*/5 * * * *", dt("2026-09-29T10:06"))
    assert cron_matches("0 9 * * *", dt("2026-09-29T09:00"))
    assert not cron_matches("0 9 * * *", dt("2026-09-29T09:01"))


def test_cron_dom_dow_or_semantics():
    # 2026-09-29 is a Tuesday. "0 9 * * 1" (Mondays) must NOT match Tuesday.
    assert not cron_matches("0 9 * * 1", dt("2026-09-29T09:00"))
    assert cron_matches("0 9 * * 2", dt("2026-09-29T09:00"))  # Tuesday
    # day-of-month OR day-of-week: 29th is a Tuesday
    assert cron_matches("0 9 29 * 2", dt("2026-09-29T09:00"))
    assert cron_matches("0 9 1 * 2", dt("2026-09-29T09:00"))  # dow matches
    assert cron_matches("0 9 29 * 1", dt("2026-09-29T09:00"))  # dom matches
    assert not cron_matches("0 9 1 * 1", dt("2026-09-29T09:00"))


def test_cron_next_simple():
    assert cron_next("*/15 * * * *", dt("2026-09-29T10:07")) == \
        dt("2026-09-29T10:15")
    # strictly after: exact hit moves to the next slot
    assert cron_next("*/15 * * * *", dt("2026-09-29T10:15")) == \
        dt("2026-09-29T10:30")


def test_cron_next_daily_rollover():
    assert cron_next("0 9 * * *", dt("2026-09-29T10:00")) == \
        dt("2026-09-30T09:00")


def test_cron_next_invalid_raises():
    with pytest.raises(ValueError):
        cron_next("not a cron", dt("2026-09-29T10:00"))


# -- one-shot parsing ----------------------------------------------------------

def test_parse_at():
    assert parse_at("@at:2026-09-30T09:00:00+03:00") == \
        datetime.fromisoformat("2026-09-30T09:00:00+03:00")
    # naive input is assumed local
    assert parse_at("@at:2026-09-30T09:00").tzinfo is not None
    with pytest.raises(ValueError):
        parse_at("*/5 * * * *")


# -- persistence ---------------------------------------------------------------

def test_add_list_remove_persist(tmp_path):
    db = tmp_path / "sched.db"
    s = Scheduler(db=db)
    s.add(Job(name="nightly", schedule="0 2 * * *", prompt="do stuff",
              model_alias="gemini", job_type="cron"))
    s.add(Job(name="legacy", schedule="* * * * *", command="echo hi"))
    jobs = {j.name: j for j in s.list()}
    assert jobs["nightly"].prompt == "do stuff"
    assert jobs["nightly"].model_alias == "gemini"
    assert jobs["legacy"].command == "echo hi"
    assert jobs["legacy"].enabled is True
    s.close()

    s2 = Scheduler(db=db)  # survives restarts
    assert {j.name for j in s2.list()} == {"nightly", "legacy"}
    assert s2.remove("nightly") is True
    assert s2.remove("nightly") is False
    assert [j.name for j in s2.list()] == ["legacy"]
    s2.close()


def test_migrates_v0_database(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE jobs (name TEXT PRIMARY KEY, schedule TEXT, "
                 "command TEXT, enabled INTEGER DEFAULT 1, last_run TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('old', '* * * * *', 'echo x', 1, '')")
    conn.commit()
    conn.close()
    s = Scheduler(db=db)
    jobs = s.list()
    assert len(jobs) == 1 and jobs[0].name == "old"
    assert jobs[0].prompt == "" and jobs[0].job_type == "cron"
    s.close()


# -- fake agent ------------------------------------------------------------------

class FakeAgent:
    def __init__(self, alias_log, text="did the thing", status="done"):
        self.alias_log = alias_log
        self.text = text
        self.status = status
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.text, status=self.status,
                               error="", run_id="fake-run")


def fake_factory(alias_log, **kw):
    def _factory(model_alias: str):
        alias_log.append(model_alias)
        return FakeAgent(alias_log, **kw)
    return _factory


# -- due calculation + execution ---------------------------------------------------

def test_run_due_executes_due_cron_job(tmp_path):
    log: list[str] = []
    s = Scheduler(db=tmp_path / "s.db", agent_factory=fake_factory(log))
    s.add(Job(name="every-minute", schedule="* * * * *",
              prompt="check things", model_alias="gemini",
              created_ts=dt("2026-09-29T10:00").isoformat()))
    results = s.run_due(now=dt("2026-09-29T10:07"))
    assert len(results) == 1
    assert results[0]["status"] == "done"
    assert results[0]["summary"] == "did the thing"
    assert log == ["gemini"]
    job = s.get("every-minute")
    assert job.last_run  # stamped
    runs = s.job_runs("every-minute")
    assert len(runs) == 1 and runs[0]["status"] == "done"
    s.close()


def test_run_due_skips_not_due(tmp_path):
    log: list[str] = []
    s = Scheduler(db=tmp_path / "s.db", agent_factory=fake_factory(log))
    s.add(Job(name="yearly", schedule="0 0 1 1 *", prompt="x",
              last_run=dt("2026-09-29T10:00").isoformat()))
    assert s.run_due(now=dt("2026-09-29T10:07")) == []
    assert log == []
    s.close()


def test_run_due_skips_disabled(tmp_path):
    log: list[str] = []
    s = Scheduler(db=tmp_path / "s.db", agent_factory=fake_factory(log))
    s.add(Job(name="off", schedule="* * * * *", prompt="x", enabled=False))
    assert s.run_due(now=dt("2026-09-29T10:07")) == []
    s.close()


def test_at_job_runs_once_then_disables(tmp_path):
    log: list[str] = []
    s = Scheduler(db=tmp_path / "s.db", agent_factory=fake_factory(log))
    s.add(Job(name="once", schedule="@at:2026-09-29T10:00", prompt="go",
              job_type="at"))
    first = s.run_due(now=dt("2026-09-29T10:07"))
    assert len(first) == 1
    assert s.get("once").enabled is False
    assert s.run_due(now=dt("2026-09-29T10:08")) == []  # never again
    s.close()


def test_at_job_in_future_not_due(tmp_path):
    s = Scheduler(db=tmp_path / "s.db",
                  agent_factory=fake_factory([]))
    s.add(Job(name="later", schedule="@at:2026-10-01T09:00", prompt="go",
              job_type="at"))
    assert s.run_due(now=dt("2026-09-29T10:07")) == []
    s.close()


def test_run_job_unknown_name(tmp_path):
    s = Scheduler(db=tmp_path / "s.db", agent_factory=fake_factory([]))
    res = s.run_job("nope")
    assert res["status"] == "error"
    s.close()


def test_run_job_records_failure(tmp_path):
    s = Scheduler(db=tmp_path / "s.db",
                  agent_factory=fake_factory([], status="timeout"))
    s.add(Job(name="flaky", schedule="* * * * *", prompt="x"))
    res = s.run_job("flaky")
    assert res["status"] == "timeout"
    assert s.job_runs("flaky")[0]["status"] == "timeout"
    s.close()


def test_agent_factory_exception_becomes_error(tmp_path):
    def boom(alias):
        raise RuntimeError("no keys")
    s = Scheduler(db=tmp_path / "s.db", agent_factory=boom)
    s.add(Job(name="broken", schedule="* * * * *", prompt="x"))
    res = s.run_job("broken")
    assert res["status"] == "error"
    assert "RuntimeError" in res["summary"]
    s.close()


# -- heartbeat ---------------------------------------------------------------------

def test_heartbeat_prompt_uses_file(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("# Mine\n- check the inbox\n")
    prompt = build_heartbeat_prompt(heartbeat_path=hb,
                                    now=dt("2026-09-29T10:00"))
    assert "check the inbox" in prompt
    assert "heartbeat" in prompt.lower()


def test_heartbeat_prompt_default_when_missing(tmp_path):
    prompt = build_heartbeat_prompt(
        heartbeat_path=tmp_path / "nope.md", now=dt("2026-09-29T10:00"))
    assert "default heartbeat checklist" in prompt
    assert "~/.agentkai/HEARTBEAT.md" in prompt


def test_heartbeat_job_runs_with_checklist_prompt(tmp_path):
    log: list[str] = []
    agents: list[FakeAgent] = []
    orig = fake_factory(log)

    def factory(alias):
        a = orig(alias)
        agents.append(a)
        return a

    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("- water the plants\n")
    s = Scheduler(db=tmp_path / "s.db", agent_factory=factory)
    s.add(Job(name="hb", schedule="*/30 * * * *", job_type="heartbeat",
              model_alias="gemini"))
    # patch the heartbeat path used by _prompt_for via monkeypatch of module const
    import agentkai.scheduler as sched_mod
    old = sched_mod.HEARTBEAT_PATH
    sched_mod.HEARTBEAT_PATH = hb
    try:
        res = s.run_job("hb")
    finally:
        sched_mod.HEARTBEAT_PATH = old
    assert res["status"] == "done"
    assert "water the plants" in agents[0].prompts[0]
    s.close()


# -- dreaming ------------------------------------------------------------------------

def test_dreaming_prompt_includes_run_summaries(tmp_path):
    runs_root = tmp_path / "runs"
    log = EventLog(root=runs_root)
    log.record("run_start", model="fake/m", prompt="test the thing")
    log.record("run_end", status="done", final_text="it worked")
    log.close()
    prompt = build_dreaming_prompt(date="2026-09-29", runs_root=runs_root,
                                   memory_dir=tmp_path / "memory")
    assert log.run_id[:8] in prompt
    assert "done" in prompt
    assert str(tmp_path / "memory" / "2026-09-29.md") in prompt


def test_dreaming_prompt_empty_day(tmp_path):
    prompt = build_dreaming_prompt(date="2026-09-29",
                                   runs_root=tmp_path / "empty-runs",
                                   memory_dir=tmp_path / "memory")
    assert "0 agent run(s)" in prompt


def test_dreaming_job_type_uses_dreaming_prompt(tmp_path):
    agents: list[FakeAgent] = []

    def factory(alias):
        a = FakeAgent([])
        agents.append(a)
        return a

    s = Scheduler(db=tmp_path / "s.db", agent_factory=factory)
    s.add(Job(name="dream", schedule="0 3 * * *", job_type="dreaming"))
    res = s.run_job("dream")
    assert res["status"] == "done"
    assert "dreaming" in agents[0].prompts[0].lower()
    s.close()


# -- CLI -------------------------------------------------------------------------------

def test_cli_scheduler_add_list_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_get_scheduler",
                        lambda: Scheduler(db=tmp_path / "cli.db"))
    runner = typer.testing.CliRunner()
    r = runner.invoke(cli_mod.app, ["scheduler", "add", "nightly",
                                   "--schedule", "0 2 * * *",
                                   "--prompt", "tidy up",
                                   "--model", "gemini"])
    assert r.exit_code == 0, r.output
    assert "nightly" in r.output
    r = runner.invoke(cli_mod.app, ["scheduler", "list"])
    assert r.exit_code == 0, r.output
    assert "nightly" in r.output and "0 2 * * *" in r.output
    r = runner.invoke(cli_mod.app, ["scheduler", "remove", "nightly"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli_mod.app, ["scheduler", "list"])
    assert "nightly" not in r.output


def test_cli_scheduler_add_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_get_scheduler",
                        lambda: Scheduler(db=tmp_path / "cli.db"))
    runner = typer.testing.CliRunner()
    r = runner.invoke(cli_mod.app, ["scheduler", "add", "hb",
                                   "--schedule", "*/30 * * * *",
                                   "--type", "heartbeat"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli_mod.app, ["scheduler", "list"])
    assert "heartbeat" in r.output


def test_cli_scheduler_add_needs_prompt_or_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_get_scheduler",
                        lambda: Scheduler(db=tmp_path / "cli.db"))
    runner = typer.testing.CliRunner()
    r = runner.invoke(cli_mod.app, ["scheduler", "add", "bad",
                                   "--schedule", "* * * * *"])
    assert r.exit_code == 2
