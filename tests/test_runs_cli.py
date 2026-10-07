"""Tests for `agentkai runs list/show`: CLI over the event log.

Runs are seeded with the real EventLog writer into a tmp AGENTKAI_HOME;
no agent loop, no network.
"""
from __future__ import annotations

import json
import os

import typer.testing

from agentkai import cli as cli_mod
from agentkai.events import EventLog


def _seed_run(root, run_id: str, model: str = "claude-sonnet",
              tools=("read_file",)) -> None:
    log = EventLog(run_id=run_id, root=root)
    log.record("run_start", model=model, prompt="summarize the repo")
    for name in tools:
        log.record("tool_call", call_id=f"c-{name}", name=name,
                   args={"path": "README.md"})
        log.record_tool_result(f"c-{name}", name, "file contents", ok=True)
    log.record("approval", tool=tools[0], decision="allow")
    log.record("run_end", status="done", final_text="here is the summary")
    log.close()


def _invoke(monkeypatch, tmp_path, args):
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    runner = typer.testing.CliRunner()
    return runner.invoke(cli_mod.app, args)


def test_runs_list_empty(tmp_path, monkeypatch):
    result = _invoke(monkeypatch, tmp_path, ["runs", "list"])
    assert result.exit_code == 0, result.output
    assert "no runs yet" in result.output


def test_runs_show_unknown_run_exits_1(tmp_path, monkeypatch):
    result = _invoke(monkeypatch, tmp_path, ["runs", "show", "nope123"])
    assert result.exit_code == 1, result.output
    assert "no run 'nope123'" in result.output


def test_runs_list_newest_first_and_limit(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    _seed_run(root, "run-older", tools=("read_file",))
    _seed_run(root, "run-newer", tools=("exec", "write_file"))
    # list_runs orders by events.jsonl mtime: make the order unambiguous.
    os.utime(root / "run-older" / "events.jsonl", (1_700_000_000,) * 2)
    os.utime(root / "run-newer" / "events.jsonl", (1_700_000_100,) * 2)

    result = _invoke(monkeypatch, tmp_path, ["runs", "list"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0].startswith("run-newer")
    assert "[done]" in lines[0] and "exec" in lines[0]
    assert lines[1].startswith("run-older")

    limited = _invoke(monkeypatch, tmp_path, ["runs", "list", "-n", "1"])
    assert limited.exit_code == 0, limited.output
    assert "run-newer" in limited.output
    assert "run-older" not in limited.output


def test_runs_list_json(tmp_path, monkeypatch):
    _seed_run(tmp_path / "runs", "run-json", model="gpt-x")
    result = _invoke(monkeypatch, tmp_path, ["runs", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-json"
    assert rows[0]["model"] == "gpt-x"
    assert rows[0]["status"] == "done"
    assert rows[0]["tool_calls"] == ["read_file"]
    assert rows[0]["final_text"] == "here is the summary"


def test_runs_show_replays_events_in_order(tmp_path, monkeypatch):
    _seed_run(tmp_path / "runs", "run-show")
    result = _invoke(monkeypatch, tmp_path, ["runs", "show", "run-show"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "run_start" in out and "claude-sonnet" in out
    assert "tool_call" in out and "read_file" in out
    assert "tool_result" in out and "[ok]" in out
    assert "approval" in out and "read_file:allow" in out
    assert "run_end" in out and "status=done" in out
    # Events print in seq order: run_start before run_end.
    assert out.index("run_start") < out.index("run_end")


def test_runs_show_json(tmp_path, monkeypatch):
    _seed_run(tmp_path / "runs", "run-show-json")
    result = _invoke(monkeypatch, tmp_path,
                     ["runs", "show", "run-show-json", "--json"])
    assert result.exit_code == 0, result.output
    evs = json.loads(result.output)
    assert [e["type"] for e in evs] == [
        "run_start", "tool_call", "tool_result", "approval", "run_end"]
    assert evs[0]["seq"] == 1 and evs[-1]["seq"] == 5
