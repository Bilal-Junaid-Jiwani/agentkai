"""Tests for agentkai.doctor: read-only environment diagnostics.

No network, no real keys — provider env vars are scrubbed and the home
dir is a tmp_path passed explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.testing

from agentkai import cli as cli_mod
from agentkai import doctor as doctor_mod
from agentkai.doctor import Check, format_json, format_text, has_failures, \
    run_checks
from agentkai.providers import KEY_ENV
from agentkai.scheduler import Job, Scheduler


def _scrub_keys(monkeypatch):
    for env_var in KEY_ENV.values():
        if env_var:
            monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AGENTKAI_IMAGE_PROVIDER", raising=False)


def test_run_checks_returns_eight_named_checks(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    checks = run_checks(tmp_path)
    assert [c.name for c in checks] == [
        "home", "config", "providers", "scheduler",
        "memory", "tools", "media", "dashboard",
    ]
    assert all(isinstance(c, Check) for c in checks)
    assert all(c.status in ("ok", "warn", "fail") for c in checks)


def test_first_run_home_is_all_warns_no_fails(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    home = tmp_path / "does-not-exist"
    checks = {c.name: c for c in run_checks(home)}
    assert checks["home"].status == "warn"
    assert checks["config"].status == "warn"
    assert checks["providers"].status == "warn"  # no default-model key
    assert checks["scheduler"].status == "warn"
    assert checks["media"].status == "warn"  # nothing configured
    assert not has_failures(run_checks(home))


def test_invalid_config_yaml_fails(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    (tmp_path / "config.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["config"].status == "fail"
    assert "does not parse" in checks["config"].detail
    assert has_failures(run_checks(tmp_path))


def test_valid_config_yaml_is_ok(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "models:\n  aliases:\n    tiny: ollama/qwen3:1b\n", encoding="utf-8")
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["config"].status == "ok"
    assert "tiny" in checks["providers"].detail


def test_providers_ok_when_default_key_set(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["providers"].status == "ok"
    assert "key set" in checks["providers"].detail


def test_scheduler_db_is_read(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    s = Scheduler(db=tmp_path / "scheduler.db",
                  agent_factory=lambda alias: None)
    s.add(Job(name="nightly", schedule="0 3 * * *", prompt="dream"))
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["scheduler"].status == "ok"
    assert "1 job(s), 1 enabled" in checks["scheduler"].detail
    s.close()


def test_corrupt_scheduler_db_fails(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    (tmp_path / "scheduler.db").write_bytes(b"not a sqlite file")
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["scheduler"].status == "fail"


def test_tools_check_lists_builtin_registry(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["tools"].status == "ok"
    assert "exec" in checks["tools"].detail
    assert "read_file" in checks["tools"].detail


def test_missing_dashboard_static_fails(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    monkeypatch.setattr(doctor_mod, "STATIC_DIR",
                        tmp_path / "no-static-here")
    checks = {c.name: c for c in run_checks(tmp_path)}
    assert checks["dashboard"].status == "fail"
    assert has_failures(run_checks(tmp_path))


def test_format_text_and_json(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    checks = run_checks(tmp_path)
    text = format_text(checks)
    assert text.splitlines()[-1].endswith("fail")
    assert "[OK] tools:" in text
    data = json.loads(format_json(checks))
    assert len(data["checks"]) == 8
    assert data["checks"][0]["name"] == "home"


def test_cli_doctor_exit_zero(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    runner = typer.testing.CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "8 checks:" in result.output


def test_cli_doctor_exit_one_on_failure(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    monkeypatch.setattr(doctor_mod, "STATIC_DIR",
                        tmp_path / "no-static-here")
    runner = typer.testing.CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "[FAIL] dashboard" in result.output


def test_cli_doctor_json_flag(tmp_path, monkeypatch):
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    runner = typer.testing.CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["checks"][0]["name"] == "home"
