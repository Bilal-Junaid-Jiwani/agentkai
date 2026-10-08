"""Tool registry: every capability the agent can call.

Tools are plain Python callables described by a JSON Schema, so any model
provider that supports function calling (via LiteLLM) can use the same set.
MCP servers attach as remote tools (see ``agentkai.mcp``).

Safety model (see research/ARCHITECTURE.md section 3):
- every tool declares a ``risk`` level: "low" | "medium" | "high"
- every tool run may pass through an optional ``gate`` callable with the
  signature ``gate(action: dict) -> "allow" | "ask" | "deny"``, where
  ``action == {"tool": name, "args": {...}, "risk": risk}``. The gate is
  duck-typed on purpose: the permissions module is built separately and
  this package must not import it.
- ``gate`` returning "deny" raises :class:`PermissionDenied`;
  "ask" raises :class:`ApprovalRequired` (a subclass) so the caller can
  surface an approval card instead of silently failing.
- tools never raise on bad input: failures are returned as strings starting
  with ``"ERROR:"`` so the agent loop can self-correct. Permission decisions
  are the one exception and always raise.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import signal
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

Risk = Literal["low", "medium", "high"]

GATE_ALLOW = "allow"
GATE_ASK = "ask"
GATE_DENY = "deny"

GateFn = Callable[[dict], str]


class PermissionDenied(Exception):
    """Raised when the permission gate denies a tool call."""


class ApprovalRequired(PermissionDenied):
    """Raised when the gate says "ask": the caller must get user approval."""


@dataclass
class Tool:
    """A single callable capability.

    Fields:
        name: registry key, e.g. "read_file".
        description: one-line help shown to the model.
        json_schema: JSON Schema for the tool's arguments (kept flat so weak
            providers can still call it reliably).
        risk: "low" | "medium" | "high".
        func: the Python callable. Receives the schema's arguments as kwargs
            and returns either a result or an "ERROR: ..." string.
    """

    name: str
    description: str
    json_schema: dict
    risk: Risk
    func: Callable[..., Any]

    @property
    def parameters(self) -> dict:
        """Backwards-compatible alias for :attr:`json_schema`."""
        return self.json_schema

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }

    def run(self, _gate: GateFn | None = None, **kwargs: Any) -> Any:
        """Execute the tool.

        ``_gate`` is reserved: an optional permission-gate callable
        ``gate(action) -> "allow" | "ask" | "deny"``. It is keyword-only by
        convention so model-supplied arguments can never collide with it.
        """
        if _gate is not None:
            decision = _gate({"tool": self.name, "args": kwargs, "risk": self.risk})
            if decision == GATE_DENY:
                raise PermissionDenied(
                    f"Permission gate denied {self.name} with args {kwargs!r}"
                )
            if decision == GATE_ASK:
                raise ApprovalRequired(
                    f"Permission gate requires approval for {self.name} "
                    f"with args {kwargs!r}"
                )
            # Any other return value (including "allow") proceeds.
        try:
            return self.func(**kwargs)
        except (PermissionDenied, ApprovalRequired):
            raise
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            return f"ERROR: {type(exc).__name__}: {exc}"


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def describe(self) -> list[dict]:
        """Public metadata for every registered tool (no callables).

        Names, one-line descriptions, and risk levels — safe to expose to a
        UI or serialize. The functions themselves stay server-side.
        """
        return [{"name": t.name, "description": t.description, "risk": t.risk}
                for t in self._tools.values()]


# ---- sandbox / path helpers ------------------------------------------------

DEFAULT_ROOT = Path("~/.agentkai/workspace").expanduser()
MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 8_000
MAX_DIR_ENTRIES = 500


def _resolve_in_roots(path: str, roots: list[Path]) -> Path:
    """Resolve *path* and ensure it stays inside one of *roots*.

    Symlinks are fully resolved, so escaping via a symlink inside the root
    is also rejected.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = roots[0] / p
    resolved = p.resolve()
    for root in roots:
        r = root.resolve()
        if resolved == r or r in resolved.parents:
            return resolved
    raise ValueError(
        f"Path escapes allowed roots: {path!r} (allowed: "
        + ", ".join(str(r) for r in roots) + ")"
    )


def _is_dangerous(command: str) -> str | None:
    """Blocklist scan. Returns a reason string, or None if the command passes.

    This is a blocklist, not a sandbox: it stops the obvious footguns, not a
    determined adversary. Real isolation belongs in a container layer.
    """
    patterns = [
        (r":\(\)\s*\{", "fork bomb"),
        (r"\bmkfs(\.\w+)?\b", "filesystem format"),
        (r"\bdd\b[^\n|;&]*\bof=/dev/", "raw disk write"),
        (r">\s*/dev/(sd|hd|nvme|vd)[a-z0-9]*", "raw disk write"),
        (r"\b(sha256sum\s+)?>\s*/dev/(mem|kmem|port)\b", "raw device write"),
        (r"\b(shutdown|reboot|halt|poweroff)\b", "power control"),
        (r"\bchmod\s+(-R\s+)?0?777\s+/\s", "permission nuke"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, command):
            return reason
    # rm with -r on / or ~ : check tokens precisely so `rm -rf ./build` passes.
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None  # unparseable; the shell will report the syntax error
    for i, tok in enumerate(tokens):
        if tok == "rm" and i + 1 < len(tokens):
            rest = tokens[i + 1 :]
            recursive = any(
                t.startswith("-") and "r" in t and not t.startswith("--")
                for t in rest
                if t.startswith("-")
            ) or "--recursive" in rest
            targets = [t for t in rest if not t.startswith("-")]
            if recursive and any(
                (t.rstrip("/") or "/") in ("/", "~") or t in ("/*", "~/*")
                for t in targets
            ):
                return "recursive delete of / or ~"
    return None


# ---- built-in tool implementations -----------------------------------------

def _make_exec(roots: list[Path], default_cwd: Path):
    def _exec(command: str, workdir: str = ".", timeout: int = 120) -> dict:
        """Run a shell command. Returns stdout/stderr/exit code."""
        reason = _is_dangerous(command)
        if reason:
            return f"ERROR: blocked dangerous command ({reason})"
        timeout = max(1, min(int(timeout), 600))
        try:
            cwd = _resolve_in_roots(workdir, roots)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not cwd.is_dir():
            return f"ERROR: workdir is not a directory: {workdir!r}"
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group, killed as one
            )
        except OSError as exc:
            return f"ERROR: could not start command: {exc}"
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the whole process group, not just the shell: children
            # the command backgrounded would otherwise outlive the timeout.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return {"exit_code": -1, "output": f"ERROR: timed out after {timeout}s"}
        out = (stdout or "")[-MAX_OUTPUT_CHARS:]
        if stderr:
            out += "\n[stderr]\n" + stderr[-2000:]
        return {"exit_code": proc.returncode, "output": out}

    return _exec


def _make_read_file(roots: list[Path]):
    def _read_file(path: str, offset: int = 0, limit: int = 200) -> dict:
        """Read a text file (``limit`` lines starting at ``offset``)."""
        try:
            p = _resolve_in_roots(path, roots)
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                return (
                    f"ERROR: file too large ({p.stat().st_size} bytes, "
                    f"limit {MAX_FILE_BYTES}); use offset/limit on a smaller file"
                )
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return f"ERROR: file not found: {path!r}"
        except IsADirectoryError:
            return f"ERROR: not a file: {path!r}. Use list_dir for directories."
        total = len(lines)
        return {"lines": lines[offset : offset + limit], "total_lines": total}

    return _read_file


def _make_write_file(roots: list[Path]):
    def _write_file(path: str, content: str, mode: str = "overwrite") -> dict:
        """Write or append a text file. Parent dirs are created as needed."""
        if mode not in ("overwrite", "append"):
            return "ERROR: mode must be 'overwrite' or 'append'"
        try:
            p = _resolve_in_roots(path, roots)
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append" and p.exists():
                content = p.read_text(encoding="utf-8", errors="replace") + content
            p.write_text(content, encoding="utf-8")
        except IsADirectoryError:
            return f"ERROR: not a file: {path!r}"
        return {"ok": True, "path": str(p)}

    return _write_file


def _make_list_dir(roots: list[Path]):
    def _list_dir(path: str = ".") -> dict:
        """List a directory's entries (name, kind, size)."""
        try:
            p = _resolve_in_roots(path, roots)
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            entries = sorted(p.iterdir(), key=lambda e: e.name)
        except FileNotFoundError:
            return f"ERROR: directory not found: {path!r}"
        except NotADirectoryError:
            return f"ERROR: not a directory: {path!r}"
        items = []
        for e in entries[:MAX_DIR_ENTRIES]:
            try:
                kind = "dir" if e.is_dir() else "file"
                size = e.stat().st_size if e.is_file() else 0
            except OSError:
                kind, size = "other", 0
            items.append({"name": e.name, "kind": kind, "size": size})
        return {
            "path": str(p),
            "entries": items,
            "truncated": len(entries) > MAX_DIR_ENTRIES,
        }

    return _list_dir


_FETCH_TIMEOUT = 20
_FETCH_MAX_BYTES = 1_000_000


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Follow redirects but re-validate every hop and cap the chain."""

    max_redirects = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if getattr(req, "_redirect_count", 0) >= self.max_redirects:
            raise urllib.error.HTTPError(
                newurl, code, "too many redirects", headers, fp
            )
        _validate_url(newurl)  # raises on disallowed target
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        new_req._redirect_count = getattr(req, "_redirect_count", 0) + 1
        return new_req


def _validate_url(url: str) -> urllib.parse.ParseResult:
    """Reject non-HTTP(S) URLs, embedded credentials, and non-public hosts."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"URL has no host: {url!r}")
    if parsed.username or parsed.password:
        # Never let credentials reach the wire or the logs.
        raise ValueError("URLs with embedded credentials are not allowed")
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Could not resolve host: {host!r}")
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"Refusing non-public address for {host!r} ({ip})")
    return parsed


def _fetch_url(url: str, timeout: int = _FETCH_TIMEOUT) -> dict:
    """GET a URL and return its text. Size-capped, no credentials in logs."""
    try:
        _validate_url(url)
    except ValueError as exc:
        return f"ERROR: {exc}"
    opener = urllib.request.build_opener(_RedirectGuard)
    req = urllib.request.Request(
        url, headers={"User-Agent": "agentkai/0.1 (+local agent)"}
    )
    try:
        with opener.open(req, timeout=min(max(int(timeout), 1), 60)) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text" not in ctype and "json" not in ctype and "xml" not in ctype:
                return (
                    f"ERROR: refusing non-text content ({ctype or 'unknown'} "
                    f"at {urllib.parse.urlparse(url).hostname})"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _FETCH_MAX_BYTES:
                    return (
                        f"ERROR: response exceeded {_FETCH_MAX_BYTES} byte cap "
                        f"at {urllib.parse.urlparse(url).hostname}"
                    )
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="replace")
            return {
                "url": resp.geturl(),
                "status": resp.status,
                "content_type": ctype,
                "text": text[:MAX_OUTPUT_CHARS],
                "truncated": len(text) > MAX_OUTPUT_CHARS,
            }
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code} fetching {urllib.parse.urlparse(url).hostname}"
    except urllib.error.URLError as exc:
        return f"ERROR: could not fetch {urllib.parse.urlparse(url).hostname}: {exc.reason}"
    except (TimeoutError, socket.timeout):
        return f"ERROR: timed out fetching {urllib.parse.urlparse(url).hostname}"


def default_registry(
    roots: list[str | Path] | None = None,
    default_cwd: str | Path | None = None,
) -> Registry:
    """Build the registry of safe built-in tools.

    Args:
        roots: allowed filesystem roots for file/shell tools. Defaults to
            ``~/.agentkai/workspace``. Relative paths resolve against the
            first root; ``..`` and symlinks cannot escape any root.
        default_cwd: default working directory for ``exec``. Defaults to the
            first root; must itself be inside ``roots``.
    """
    root_paths = [Path(r).expanduser() for r in (roots or [DEFAULT_ROOT])]
    cwd = Path(default_cwd).expanduser() if default_cwd else root_paths[0]
    cwd_resolved = cwd.resolve()
    if not any(
        cwd_resolved == r.resolve() or r.resolve() in cwd_resolved.parents
        for r in root_paths
    ):
        raise ValueError(f"default_cwd {cwd} is outside allowed roots {root_paths}")
    for r in root_paths:
        r.mkdir(parents=True, exist_ok=True)

    exec_fn = _make_exec(root_paths, cwd_resolved)

    def _exec_default(command: str, workdir: str = ".", timeout: int = 120) -> dict:
        # workdir="." means the configured default cwd, not the process cwd.
        if workdir == ".":
            workdir = str(cwd_resolved)
        return exec_fn(command, workdir=workdir, timeout=timeout)

    r = Registry()
    r.register(Tool(
        name="exec",
        description=(
            "Run a shell command on the agent's computer and get output. "
            "Working directory defaults to the agent workspace and cannot "
            "escape the allowed roots. Obviously destructive commands are blocked."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
        risk="high",
        func=_exec_default,
    ))
    r.register(Tool(
        name="read_file",
        description="Read a text file from the agent workspace (offset/limit paging).",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
        risk="low",
        func=_make_read_file(root_paths),
    ))
    r.register(Tool(
        name="write_file",
        description=(
            "Write or append a text file inside the agent workspace. "
            "Cannot write outside the allowed roots."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"]},
            },
            "required": ["path", "content"],
        },
        risk="medium",
        func=_make_write_file(root_paths),
    ))
    r.register(Tool(
        name="list_dir",
        description="List a directory's entries inside the agent workspace.",
        json_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        risk="low",
        func=_make_list_dir(root_paths),
    ))
    r.register(Tool(
        name="fetch_url",
        description=(
            "Fetch a web page over http/https and return its text. "
            "Size-capped; refuses non-public hosts and embedded credentials."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["url"],
        },
        risk="medium",
        func=_fetch_url,
    ))
    return r
