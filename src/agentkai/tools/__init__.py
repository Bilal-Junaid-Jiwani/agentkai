"""Tool registry: every capability the agent can call.

Tools are plain Python callables with a JSON-schema description, so any model
provider that supports function calling (via LiteLLM) can use the same set.
MCP servers can be attached as remote tools (see mcp_client.py).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: callable

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, **kwargs):
        return self.func(**kwargs)


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)


# ---- built-in tools -------------------------------------------------------

def _exec(command: str, workdir: str = ".", timeout: int = 120) -> dict:
    """Run a shell command. Returns stdout/stderr/exit code."""
    try:
        p = subprocess.run(command, shell=True, cwd=workdir, capture_output=True,
                           text=True, timeout=timeout)
        out = p.stdout[-4000:] + (p.stderr[-2000:] if p.stderr else "")
        return {"exit_code": p.returncode, "output": out}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": f"timed out after {timeout}s"}


def _read_file(path: str, limit: int = 200) -> dict:
    """Read a text file (first `limit` lines)."""
    try:
        lines = Path(path).expanduser().read_text().splitlines()
        return {"lines": lines[:limit], "total_lines": len(lines)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    """Write/append a text file."""
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            prev = p.read_text() if p.exists() else ""
            p.write_text(prev + content)
        else:
            p.write_text(content)
        return {"ok": True, "path": str(p)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def default_registry() -> Registry:
    r = Registry()
    r.register(Tool(
        name="exec",
        description="Run a shell command on the agent's computer and get output.",
        parameters={"type": "object",
                     "properties": {"command": {"type": "string"},
                                    "workdir": {"type": "string"},
                                    "timeout": {"type": "integer"}},
                     "required": ["command"]},
        func=_exec))
    r.register(Tool(
        name="read_file",
        description="Read a text file from the filesystem.",
        parameters={"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["path"]},
        func=_read_file))
    r.register(Tool(
        name="write_file",
        description="Write or append a text file on the filesystem.",
        parameters={"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "content": {"type": "string"},
                                    "mode": {"type": "string",
                                             "enum": ["overwrite", "append"]}},
                     "required": ["path", "content"]},
        func=_write_file))
    return r
