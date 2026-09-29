"""Markdown-file memory: SOUL.md / USER.md / MEMORY.md / daily logs.

Same pattern as OpenClaw: plain markdown files the agent reads at startup and
appends to as it learns. Human-editable, git-friendly, no database needed.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path


class Memory:
    def __init__(self, root: str | Path = "~/.agentkai/memory"):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / name

    def read(self, name: str) -> str:
        p = self._path(name)
        return p.read_text() if p.exists() else ""

    def write(self, name: str, content: str) -> None:
        self._path(name).write_text(content)

    def append(self, name: str, content: str) -> None:
        p = self._path(name)
        prev = p.read_text() if p.exists() else ""
        p.write_text(prev + content)

    def log_day(self, line: str) -> None:
        """Append a timestamped line to today's daily log."""
        self.append(f"{date.today().isoformat()}.md", f"- {line}\n")

    def context_block(self) -> str:
        """Assemble the memory files into a context block for the system prompt."""
        parts = []
        for name in ("SOUL.md", "USER.md", "MEMORY.md"):
            text = self.read(name).strip()
            if text:
                parts.append(f"[{name}]\n{text}")
        return "\n\n".join(parts)
