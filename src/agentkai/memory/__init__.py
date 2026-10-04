"""Markdown-file memory: SOUL.md / USER.md / MEMORY.md / daily logs / people / groups.

Same pattern as OpenClaw: plain markdown files the agent reads at startup and
appends to as it learns. Human-editable, git-friendly, no database needed.
Retrieval is keyword search over the files (no vector DB in v1).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

SECTIONS = {
    "soul": "SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "identity": "IDENTITY.md",
}


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "unnamed"


class Memory:
    def __init__(self, root: str | Path = "~/.agentkai/memory"):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- low-level file access -------------------------------------------

    def _path(self, name: str) -> Path:
        return self.root / name

    def _resolve_section(self, section: str) -> str:
        return SECTIONS.get(section.strip().lower(), section)

    def read(self, name: str) -> str:
        p = self._path(name)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write(self, section: str, text: str) -> None:
        """Write a whole section file.

        ``section`` is one of "soul" | "user" | "memory" | "identity", or a
        literal filename such as "SOUL.md" (backwards compatible).
        """
        self._path(self._resolve_section(section)).write_text(text, encoding="utf-8")

    def replace(self, name: str, text: str) -> tuple[bool, str | None]:
        """Replace a root-level memory file, keeping the previous version.

        The old content is preserved via an atomic rename to a
        ``<name>.bak`` sidecar before the new content is written.

        Returns ``(created, backup)``: ``created`` is True when the file did
        not exist before; ``backup`` is the backup filename (``"<name>.bak"``)
        or None when there was no previous version to keep.
        """
        p = self._path(name)
        created = not p.exists()
        backup: str | None = None
        if not created:
            backup = name + ".bak"
            p.replace(self._path(backup))  # atomic: old content is the .bak
        p.write_text(text, encoding="utf-8")
        return created, backup

    def append(self, name: str, content: str) -> None:
        p = self._path(name)
        prev = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(prev + content, encoding="utf-8")

    # ---- daily log --------------------------------------------------------

    def _daily_name(self, day: date | None = None) -> str:
        return f"{(day or date.today()).isoformat()}.md"

    def append_daily(self, text: str, day: date | None = None) -> None:
        """Append a timestamped line to a daily log (default: today)."""
        stamp = datetime.now().strftime("%H:%M")
        self.append(self._daily_name(day), f"- [{stamp}] {text}\n")

    def log_day(self, line: str) -> None:
        """Backwards-compatible alias for :meth:`append_daily`."""
        self.append_daily(line)

    def read_daily(self, day: date | None = None) -> str:
        return self.read(self._daily_name(day))

    # ---- people / groups ---------------------------------------------------

    def _index_path(self, kind: str) -> Path:
        return self.root / kind / "INDEX.md"

    def _ensure_index(self, kind: str) -> None:
        d = self.root / kind
        d.mkdir(parents=True, exist_ok=True)
        idx = self._index_path(kind)
        if not idx.exists():
            idx.write_text(f"# {kind.title()} Index\n\n", encoding="utf-8")

    def _index_entries(self, kind: str) -> dict[str, str]:
        """Parse ``<kind>/INDEX.md`` into {display name: relative file path}."""
        self._ensure_index(kind)
        entries: dict[str, str] = {}
        for line in self._index_path(kind).read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*-\s+\*\*(.+?)\*\*\s+—\s+`([^`]+)`", line)
            if m:
                entries[m.group(1).strip()] = m.group(2).strip()
        return entries

    def _add_entry(self, kind: str, name: str, notes: str = "") -> Path:
        self._ensure_index(kind)
        entries = self._index_entries(kind)
        if name in entries:
            return self.root / entries[name]
        rel = f"{kind}/{_slug(name)}.md"
        page = self.root / rel
        if not page.exists():
            page.write_text(f"# {name}\n\n{notes.strip()}\n" if notes.strip()
                            else f"# {name}\n", encoding="utf-8")
        with self._index_path(kind).open("a", encoding="utf-8") as fh:
            fh.write(f"- **{name}** — `{rel}`\n")
        return page

    def add_person(self, name: str, notes: str = "") -> Path:
        """Add a person page (idempotent) and return its path."""
        return self._add_entry("people", name, notes)

    def add_group(self, name: str, notes: str = "") -> Path:
        """Add a group page (idempotent) and return its path."""
        return self._add_entry("groups", name, notes)

    def person(self, name: str) -> str:
        """Look up a person page by name; create a stub if it doesn't exist.

        Returns the page's markdown content.
        """
        for known, rel in self._index_entries("people").items():
            if known.lower() == name.strip().lower():
                return (self.root / rel).read_text(encoding="utf-8")
        page = self._add_entry("people", name.strip())
        return page.read_text(encoding="utf-8")

    def group(self, name: str) -> str:
        """Look up a group page by name; create a stub if it doesn't exist."""
        for known, rel in self._index_entries("groups").items():
            if known.lower() == name.strip().lower():
                return (self.root / rel).read_text(encoding="utf-8")
        page = self._add_entry("groups", name.strip())
        return page.read_text(encoding="utf-8")

    def people(self) -> dict[str, str]:
        """Return {display name: relative path} for all known people."""
        return self._index_entries("people")

    def groups(self) -> dict[str, str]:
        return self._index_entries("groups")

    # ---- retrieval --------------------------------------------------------

    def _iter_markdown(self):
        for p in sorted(self.root.rglob("*.md")):
            if p.is_file():
                yield p

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Keyword search over all memory files.

        Returns up to ``limit`` hits ranked by score, each a dict with
        ``file`` (path relative to the memory root), ``lineno``,
        ``line``, and ``score``. Reference a hit as ``f"{file}:{lineno}"``.
        """
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        hits: list[dict] = []
        for path in self._iter_markdown():
            rel = str(path.relative_to(self.root))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name_boost = sum(2.0 for t in terms if t in rel.lower())
            for lineno, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                count = sum(low.count(t) for t in terms)
                if not count and not name_boost:
                    continue
                score = float(count) + name_boost * 0.1
                if rel in ("SOUL.md", "USER.md", "MEMORY.md"):
                    score *= 1.2
                if line.lstrip().startswith("#"):
                    score *= 1.5
                hits.append({"file": rel, "lineno": lineno,
                             "line": line.strip(), "score": round(score, 2)})
        hits.sort(key=lambda h: (-h["score"], h["file"], h["lineno"]))
        return hits[:limit]

    # ---- prompt assembly ---------------------------------------------------

    def context_block(self) -> str:
        """Assemble the memory files into a context block for the system prompt."""
        parts = []
        for name in ("SOUL.md", "USER.md", "MEMORY.md"):
            text = self.read(name).strip()
            if text:
                parts.append(f"[{name}]\n{text}")
        return "\n\n".join(parts)
