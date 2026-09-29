"""Shared helpers for bundled skill tools.py modules."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def home_dir(config: dict | None) -> Path:
    """Agent home dir: ``config["home"]``, else $AGENTKAI_HOME, else ~/.agentkai."""
    if config:
        home = config.get("home")
        if home:
            return Path(home).expanduser()
    override = os.environ.get("AGENTKAI_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.agentkai").expanduser()


def get_token(config: dict | None, env_var: str,
              filename: str) -> str | None:
    """Read a bearer token: env var first, then ``~/<filename>``.

    The file may be ``{"access_token": "..."}`` or ``{"token": "..."}``.
    Returns None when nothing usable is found. Never logs the value.
    """
    raw = os.environ.get(env_var)
    if raw and raw.strip():
        return raw.strip()
    path = home_dir(config) / filename
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            token = data.get("access_token") or data.get("token")
            if token and str(token).strip():
                return str(token).strip()
    return None


def auth_error(skill: str, env_var: str, filename: str) -> str:
    """Honest 'not configured' message pointing at SKILL.md setup steps."""
    return (
        f"ERROR: {skill} is not configured. Set the {env_var} environment "
        f"variable, or create ~/{home_dir(None).name}/{filename} containing "
        f'{{\n  "access_token": "<your-token>"\n}}. '
        f"See the skill's SKILL.md for how to obtain a token — "
        f"this skill does not work without one."
    )


def test_transport(config: dict | None) -> Any | None:
    """Test hook: ``config["transport"]`` is forwarded to HttpClient."""
    return (config or {}).get("transport")
