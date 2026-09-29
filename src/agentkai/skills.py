"""Skills framework: SKILL.md-based playbooks with optional tools.

A skill is a directory containing ``SKILL.md`` (YAML frontmatter: name,
description, version, when-to-use + markdown instructions) and optionally
``tools.py`` exposing ``get_tools(config) -> list[Tool]``.

Discovery order:
1. bundled skills in ``agentkai.skills_bundle`` (shipped with the package),
2. user skills in ``~/.agentkai/skills/`` (or ``$AGENTKAI_HOME/skills``).

A user skill with the same name as a bundled one shadows it.

The agent uses the loader at startup (see research/ARCHITECTURE.md §7):
``system_context()`` injects every skill's SKILL.md text into the system
prompt, ``tools()`` registers all skill tools, and ``match(query)`` picks
relevant skills with a keyword heuristic.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .tools import Tool

BUNDLED_DIR = Path(__file__).parent / "skills_bundle"
MANIFEST_NAME = "manifest.json"


class SkillError(Exception):
    """Malformed SKILL.md, bad install source, unknown skill, …"""


def agentkai_home() -> Path:
    """``$AGENTKAI_HOME`` or ``~/.agentkai``. Env is read fresh each call."""
    override = os.environ.get("AGENTKAI_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.agentkai").expanduser()


def user_skills_dir() -> Path:
    return agentkai_home() / "skills"


# ---- SKILL.md parsing -------------------------------------------------------

REQUIRED_FRONTMATTER = ("name", "description", "version")


def parse_skill_md(path: str | Path) -> dict:
    """Parse SKILL.md into {"name","description","version","when","instructions"}.

    Raises SkillError on missing frontmatter or missing required fields.
    """
    text = Path(path).read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        raise SkillError(
            f"{path}: SKILL.md must start with a YAML frontmatter block "
            f"delimited by '---' lines")
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: invalid frontmatter YAML: {exc}")
    if not isinstance(front, dict):
        raise SkillError(f"{path}: frontmatter must be a mapping")
    missing = [k for k in REQUIRED_FRONTMATTER if not front.get(k)]
    if missing:
        raise SkillError(
            f"{path}: frontmatter missing required fields: "
            + ", ".join(missing))
    when = front.get("when", "")
    if isinstance(when, list):
        when = ", ".join(str(w) for w in when)
    return {
        "name": str(front["name"]).strip(),
        "description": str(front["description"]).strip(),
        "version": str(front["version"]).strip(),
        "when": str(when or "").strip(),
        "instructions": match.group(2).strip(),
    }


@dataclass
class Skill:
    """One loaded skill: metadata + instructions + tools."""

    name: str
    description: str
    version: str
    when: str
    path: Path
    instructions: str
    tools: list[Tool] = field(default_factory=list)
    source: str = "bundled"  # "bundled" | "user"

    def context_block(self) -> str:
        return (f"## Skill: {self.name} (v{self.version})\n"
                f"{self.description}\n\n{self.instructions}")


def _load_tools_py(skill_dir: Path, module_name: str,
                   config: dict) -> list[Tool]:
    """Import <skill_dir>/tools.py and call get_tools(config)."""
    tools_py = skill_dir / "tools.py"
    if not tools_py.exists():
        return []
    spec = importlib.util.spec_from_file_location(module_name, tools_py)
    if spec is None or spec.loader is None:
        raise SkillError(f"{skill_dir}: could not load tools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SkillError(f"{skill_dir}: tools.py raised on import: {exc}")
    get_tools = getattr(module, "get_tools", None)
    if get_tools is None:
        return []
    try:
        tools = get_tools(config)
    except Exception as exc:
        raise SkillError(f"{skill_dir}: get_tools(config) failed: {exc}")
    if not isinstance(tools, list) or not all(
            isinstance(t, Tool) for t in tools):
        raise SkillError(
            f"{skill_dir}: get_tools must return list[Tool]")
    return tools


# ---- loader -----------------------------------------------------------------

class SkillLoader:
    """Discover, load, and query skills."""

    def __init__(self, user_dir: str | Path | None = None,
                 bundled_dir: str | Path | None = None,
                 config: dict | None = None) -> None:
        self.user_dir = Path(user_dir) if user_dir else user_skills_dir()
        self.bundled_dir = (Path(bundled_dir) if bundled_dir
                            else BUNDLED_DIR)
        self.config = {"home": agentkai_home(), **(config or {})}
        self._cache: dict[str, Skill] = {}

    def discover(self) -> list[tuple[str, str, Path]]:
        """[(name, source, path)] — user skills shadow bundled ones."""
        found: dict[str, tuple[str, Path]] = {}
        for source, root in (("bundled", self.bundled_dir),
                             ("user", self.user_dir)):
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if (not child.is_dir() or child.name.startswith("_")
                        or not (child / "SKILL.md").exists()):
                    continue
                found[child.name] = (source, child)
        return [(name, source, path)
                for name, (source, path) in sorted(found.items())]

    def load(self, name: str) -> Skill:
        """Load one skill (cached): parse SKILL.md + import tools.py."""
        if name in self._cache:
            return self._cache[name]
        entries = {n: (s, p) for n, s, p in self.discover()}
        if name not in entries:
            raise SkillError(f"unknown skill {name!r}")
        source, path = entries[name]
        meta = parse_skill_md(path / "SKILL.md")
        if meta["name"] != name:
            raise SkillError(
                f"{path}: directory name {name!r} != frontmatter name "
                f"{meta['name']!r}")
        module_name = (f"agentkai.skills_bundle.{name}.tools"
                       if source == "bundled"
                       else f"agentkai_user_skill_{name}.tools")
        tools = _load_tools_py(path, module_name, self.config)
        skill = Skill(name=meta["name"], description=meta["description"],
                      version=meta["version"], when=meta["when"],
                      path=path, instructions=meta["instructions"],
                      tools=tools, source=source)
        self._cache[name] = skill
        return skill

    def all(self) -> list[Skill]:
        return [self.load(name) for name, _, _ in self.discover()]

    def tools(self) -> list[Tool]:
        """Every tool from every skill (names must be unique)."""
        tools: list[Tool] = []
        seen: set[str] = set()
        for skill in self.all():
            for tool in skill.tools:
                if tool.name in seen:
                    raise SkillError(
                        f"duplicate tool name {tool.name!r} "
                        f"(skill {skill.name!r})")
                seen.add(tool.name)
                tools.append(tool)
        return tools

    def system_context(self) -> str:
        """SKILL.md texts concatenated for system-prompt injection."""
        blocks = [s.context_block() for s in self.all()]
        if not blocks:
            return ""
        return "# Skills\n\n" + "\n\n---\n\n".join(blocks)

    def match(self, query: str, limit: int = 3) -> list[Skill]:
        """Keyword heuristic: rank skills by token overlap with the query."""
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        if not tokens:
            return []
        scored: list[tuple[int, Skill]] = []
        for skill in self.all():
            haystack = " ".join(
                [skill.name, skill.description, skill.when]).lower()
            hay_tokens = set(re.findall(r"[a-z0-9]+", haystack))
            score = len(tokens & hay_tokens)
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda s: (-s[0], s[1].name))
        return [s for _, s in scored[:limit]]


# ---- install / remove -------------------------------------------------------

def _skill_name_from_source(source: str, explicit: str | None) -> str:
    """Install name: explicit --name, else SKILL.md frontmatter name, else
    the source's basename. Always validated."""
    if explicit and explicit.strip():
        name = explicit.strip()
    else:
        name = ""
        local = Path(source).expanduser()
        if local.is_dir() and (local / "SKILL.md").exists():
            try:
                name = parse_skill_md(local / "SKILL.md")["name"]
            except SkillError:
                name = ""
        if not name:
            cleaned = source.rstrip("/").removesuffix(".git")
            name = cleaned.split("/")[-1].split(":")[-1]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise SkillError(
            f"invalid skill name {name!r}: use lowercase letters, digits, "
            f"'-' or '_'")
    return name


def _looks_like_git_url(source: str) -> bool:
    return (source.startswith(("http://", "https://", "git@", "ssh://"))
            or re.fullmatch(r"[\w.-]+/[\w.-]+", source) is not None)


def install_skill(source: str, name: str | None = None,
                  force: bool = False) -> Path:
    """Install a skill into the user skills dir.

    ``source`` is a local directory or a git URL (``owner/repo`` expands to
    github.com). Writes ``manifest.json`` with provenance. Returns the
    installed path. Nothing is committed or pushed.
    """
    skill_name = _skill_name_from_source(source, name)
    dest = user_skills_dir() / skill_name
    if dest.exists():
        if not force:
            raise SkillError(
                f"skill {skill_name!r} already installed at {dest} "
                f"(use force=True to overwrite)")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    local = Path(source).expanduser()
    try:
        if local.is_dir():
            shutil.copytree(local, dest)
        elif _looks_like_git_url(source):
            url = source
            if re.fullmatch(r"[\w.-]+/[\w.-]+", source):
                url = f"https://github.com/{source}.git"
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(dest)],
                capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise SkillError(
                    f"git clone failed for {url!r}: "
                    f"{proc.stderr.strip()[:300]}")
        else:
            raise SkillError(
                f"install source must be a local directory or git URL, "
                f"got {source!r}")
        if not (dest / "SKILL.md").exists():
            raise SkillError(
                f"{source!r} is not a skill: no SKILL.md found at top level")
        manifest = {
            "name": skill_name,
            "source": source,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "version": parse_skill_md(dest / "SKILL.md")["version"],
        }
        (dest / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def remove_skill(name: str) -> None:
    """Remove a user-installed skill. Bundled skills cannot be removed."""
    target = user_skills_dir() / name
    if not target.is_dir():
        bundled = BUNDLED_DIR / name
        if (bundled / "SKILL.md").exists():
            raise SkillError(
                f"skill {name!r} is bundled with agentkai and cannot be "
                f"removed")
        raise SkillError(f"skill {name!r} is not installed")
    shutil.rmtree(target)


def list_skills() -> list[dict]:
    """Installed + bundled skills with metadata (no tool imports)."""
    loader = SkillLoader()
    out = []
    for name, source, path in loader.discover():
        try:
            meta = parse_skill_md(path / "SKILL.md")
        except SkillError as exc:
            out.append({"name": name, "source": source, "error": str(exc)})
            continue
        manifest: dict[str, Any] = {}
        mf = path / MANIFEST_NAME
        if mf.exists():
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        out.append({"name": meta["name"], "description": meta["description"],
                    "version": meta["version"], "source": source,
                    "tools": _count_tools_safely(path),
                    "manifest": manifest})
    return out


def _count_tools_safely(path: Path) -> int | None:
    """Best-effort tool count without importing tools.py."""
    tools_py = path / "tools.py"
    if not tools_py.exists():
        return 0
    try:
        text = tools_py.read_text(encoding="utf-8")
        names = re.findall(r'(?:name=|_t\()"([a-z0-9_]+)"', text)
        return len(set(names))
    except OSError:
        return None


__all__ = [
    "Skill", "SkillError", "SkillLoader", "agentkai_home", "user_skills_dir",
    "parse_skill_md", "install_skill", "remove_skill", "list_skills",
    "BUNDLED_DIR",
]
