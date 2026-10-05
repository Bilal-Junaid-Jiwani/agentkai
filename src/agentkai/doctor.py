"""`agentkai doctor`: environment diagnostics.

Read-only checks that verify a setup without starting anything: the agent
home directory, the config file, provider API keys, the scheduler database,
the memory tree, the tool registry, image media providers, and the bundled
dashboard static assets.

Prints one line per check — ``[OK]``, ``[WARN]``, or ``[FAIL]`` — and exits
with code 1 when anything FAILs. Doctor never writes anything except to a
scratch temp dir for the registry probe; it never mutates the agent home.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .dashboard.server import DEFAULT_MODEL_ALIAS, STATIC_DIR
from .media import LocalSDProvider, MediaError, SD_BASE_URL, \
    select_image_provider
from .memory import Memory
from .providers import ALIASES, ProviderConfig, resolve_model
from .tools import default_registry


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail}


def _home() -> Path:
    # Honor AGENTKAI_HOME exactly like media.agentkai_home() does.
    override = os.environ.get("AGENTKAI_HOME")
    return Path(override).expanduser() if override \
        else Path("~/.agentkai").expanduser()


def run_checks(home: Path | None = None) -> list[Check]:
    """Run every check against ``home`` (default: the agent home).

    Each check is defensive: nothing here may raise. An unexpected error
    becomes a [FAIL] check with the exception text as detail.
    """
    home = Path(home).expanduser() if home else _home()
    checks: list[Check] = []

    def add(name: str, fn) -> None:
        try:
            checks.append(fn())
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            checks.append(Check(name, "fail",
                                f"{type(exc).__name__}: {exc}"))

    add("home", lambda: _check_home(home))
    add("config", lambda: _check_config(home))
    add("providers", lambda: _check_providers(home))
    add("scheduler", lambda: _check_scheduler(home))
    add("memory", lambda: _check_memory(home))
    add("tools", _check_tools)
    add("media", lambda: _check_media(home))
    add("dashboard", _check_dashboard)
    return checks


# -- individual checks -------------------------------------------------------


def _check_home(home: Path) -> Check:
    if not home.exists():
        return Check("home", "warn",
                     f"{home} does not exist yet (first run will create it)")
    if not home.is_dir():
        return Check("home", "fail", f"{home} exists but is not a directory")
    return Check("home", "ok", str(home))


def _check_config(home: Path) -> Check:
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return Check("config", "warn",
                     "config.yaml not present (defaults will be used)")
    try:
        import yaml  # local import: only needed when a config exists
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return Check("config", "fail",
                     f"config.yaml does not parse: {exc}")
    if not isinstance(data, dict):
        return Check("config", "fail", "config.yaml must be a YAML mapping")
    return Check("config", "ok", f"parsed ({len(data)} top-level keys)")


def _check_providers(home: Path) -> Check:
    cfg = ProviderConfig.load(home / "config.yaml")
    status = cfg.key_status()
    aliases = {**ALIASES, **cfg.extra_aliases}
    rows = []
    for alias in sorted(aliases):
        model = resolve_model(alias, cfg.extra_aliases)
        prefix = model.split("/", 1)[0].split(":", 1)[0].lower()
        rows.append(f"{alias} -> {model} "
                    f"(key: {status.get(prefix, 'missing')})")
    default = resolve_model(DEFAULT_MODEL_ALIAS, cfg.extra_aliases)
    default_prefix = default.split("/", 1)[0].split(":", 1)[0].lower()
    note = "; ".join(rows)
    if status.get(default_prefix) != "set":
        return Check("providers", "warn",
                     f"default model '{DEFAULT_MODEL_ALIAS}' "
                     f"({default}) has no API key — "
                     f"set the key before running. Models: {note}")
    return Check("providers", "ok", f"default key set. Models: {note}")


def _check_scheduler(home: Path) -> Check:
    db = home / "scheduler.db"
    if not db.exists():
        return Check("scheduler", "warn",
                     "scheduler.db not present (no jobs yet)")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return Check("scheduler", "fail", f"cannot open scheduler.db: {exc}")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "name" not in cols:
            return Check("scheduler", "fail",
                         "scheduler.db has no usable jobs table")
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        enabled = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE enabled=1").fetchone()[0]
        recent_fail = ""
        try:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=1)).isoformat()
            failed = conn.execute(
                "SELECT COUNT(*) FROM job_runs WHERE status NOT IN "
                "('done', 'skipped') AND started_ts >= ?",
                (since,)).fetchone()[0]
            recent_fail = f", {failed} failed in last 24h"
        except sqlite3.Error:
            pass  # job_runs table may not exist on older databases
        return Check("scheduler", "ok",
                     f"{total} job(s), {enabled} enabled{recent_fail}")
    except sqlite3.Error as exc:
        return Check("scheduler", "fail", f"cannot read scheduler.db: {exc}")
    finally:
        conn.close()


def _check_memory(home: Path) -> Check:
    mem = Memory(home / "memory")
    n = sum(1 for _ in mem.root.rglob("*.md"))
    return Check("memory", "ok", f"{n} markdown file(s) under {mem.root}")


def _check_tools() -> Check:
    with tempfile.TemporaryDirectory(prefix="agentkai-doctor-") as tmp:
        registry = default_registry(roots=[tmp], default_cwd=tmp)
        described = registry.describe()
    names = ", ".join(t["name"] for t in described)
    risks = {t["risk"] for t in described}
    bad = risks - {"low", "medium", "high"}
    if bad:
        return Check("tools", "fail", f"unknown risk level(s): {sorted(bad)}")
    return Check("tools", "ok",
                 f"{len(described)} built-in tools ({names})")


def _check_media(home: Path) -> Check:
    try:
        provider = select_image_provider(probe_local=False)
        return Check("media", "ok",
                     f"image provider: {type(provider).__name__}")
    except MediaError:
        pass  # nothing configured explicitly — check for a local server
    base = (os.environ.get("AGENTKAI_SD_URL") or SD_BASE_URL).rstrip("/")
    try:
        if LocalSDProvider(base).is_available():
            return Check("media", "ok",
                         f"local Stable Diffusion answering at {base}")
    except Exception:
        pass
    return Check("media", "warn",
                 "no image provider: set OPENAI_API_KEY (paid) or run a "
                 "local Stable Diffusion server (see docs/media.md)")


def _check_dashboard() -> Check:
    index = STATIC_DIR / "index.html"
    if not STATIC_DIR.is_dir():
        return Check("dashboard", "fail",
                     f"dashboard static dir missing: {STATIC_DIR}")
    if not index.exists():
        return Check("dashboard", "fail",
                     "dashboard index.html missing (broken packaging?)")
    return Check("dashboard", "ok", f"static assets present ({STATIC_DIR})")


# -- formatting --------------------------------------------------------------


def format_text(checks: Sequence[Check]) -> str:
    tag = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = [f"{tag.get(c.status, '[?]')} {c.name}: {c.detail}"
             for c in checks]
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    lines.append(f"{len(checks)} checks: "
                 f"{len(checks) - fails - warns} ok, {warns} warn, "
                 f"{fails} fail")
    return "\n".join(lines)


def format_json(checks: Sequence[Check]) -> str:
    return json.dumps({"checks": [c.as_dict() for c in checks]}, indent=2)


def has_failures(checks: Sequence[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
