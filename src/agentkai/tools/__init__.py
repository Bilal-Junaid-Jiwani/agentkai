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

import fnmatch
import ipaddress
import os
import posixpath
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
        thread_affinity: when True, the agent loop always runs this tool on
            a dedicated per-tool thread instead of a fresh thread per call.
            Required for tools whose underlying library is thread-affine
            (e.g. Playwright's sync API: a persistent browser session must
            be driven from the thread that created it).
    """

    name: str
    description: str
    json_schema: dict
    risk: Risk
    func: Callable[..., Any]
    thread_affinity: bool = False

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


_SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "ash"}
_SEPARATORS = {";", "&&", "||", "|", "&"}
# curl/wget output piped straight into a shell: the classic remote-code
# one-liner. The shell name must be the piped command word, so a pipe into
# e.g. sha256sum does not match.
_PIPE_TO_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?['\"]?[\w./-]*"
    r"(?:sh|bash|zsh|dash|fish|ksh)\b")


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _normalize_path_spelling(tok: str) -> str:
    """Collapse dot segments / repeated separators the way the shell will.

    The shell resolves ``//``, ``/./`` and ``/../`` before acting, so
    ``rm -rf /./`` wipes ``/`` exactly like ``rm -rf /`` — but the raw
    spelling misses every literal ``/``/home check. ``~``/``$HOME``/
    ``${HOME}`` prefixes are expanded to the real home path first so
    ``$HOME/..`` (the parent of home) is not confused with home itself.

    Two more shell spellings of home that the old expansion missed
    (the ``..`` handling below is normalization only: ``$HOME/..``
    resolves to the parent of home, which ``_is_root_or_home`` guards
    separately because that parent holds every home):

    - ``~user`` expands to that account's home (``pwd`` lookup), so
      ``rm -rf ~root`` / ``rm -rf ~<current-user>`` wipes a home dir
      while the old code normalized it to the nonsense path
      ``<home>user``. An unknown username is left untouched: the shell
      leaves it literal too, so ``rm`` fails harmlessly on it.
    - ``${HOME:-word}`` / ``${HOME-word}`` expand to ``$HOME`` when it
      is set (the normal case), and ``${HOME:+word}`` / ``${HOME+word}``
      expand to ``word``; ``${HOME:=word}`` / ``${HOME:?word}`` (and
      their ``-`` forms) yield ``$HOME`` when it is set. Each is
      expanded the same way before the protected-path comparison.
    - ``${HOME:offset[:length]}`` substring expansion can spell a
      guarded dir from a prefix of home: ``${HOME:0:5}`` is ``/home``
      for a ``/home/<user>`` home, and ``${HOME:0}`` is home itself.
      (``${HOME:-5}`` is the default-value form above, not a substring:
      a negative substring offset needs the space, ``${HOME: -5}``.)
    - ``${HOME%pat}`` / ``${HOME%%pat}`` suffix removal likewise
      spells guarded dirs: ``${HOME%/*}`` is ``/home`` (the directory
      holding every home) and ``${HOME%%/*}`` is empty. Prefix
      removal (``${HOME#pat}`` / ``${HOME##pat}``) yields a relative
      path (``home/<user>``), which stays out of scope exactly like
      any other relative token.

    Non-absolute tokens are returned untouched: a relative path can never
    be ``/`` or the home directory.
    """
    t = tok
    if t.startswith("${HOME"):
        # ${HOME:offset[:length]} substring expansion and ${HOME#/%pat}
        # pattern removal are handled before the <op>word forms below:
        # neither starts with one of those operators, so the loop would
        # leave them untouched (and the shell would not).
        m = re.match(r"^\$\{HOME:(?:(\d+)| +(-?\d+))(?:\: *(-?\d+))?\}",
                     t)
        if m:
            home = str(Path.home())
            offset = int(m.group(1) if m.group(1) is not None
                         else m.group(2))
            length = m.group(3)
            if length is None:
                t = home[offset:] + t[m.end():]
            elif int(length) >= 0:
                t = home[offset:offset + int(length)] + t[m.end():]
            else:  # negative length: omit that many chars from the end
                t = home[offset:int(length)] + t[m.end():]
            if not t.startswith("/"):
                return t
            norm = posixpath.normpath(t)
            return "/" if norm == "//" else norm
        m = re.match(r"^\$\{HOME([#%])(\1)?([^\}]*)\}(.*)$", t)
        if m:
            op, double, pat = m.group(1), m.group(2), m.group(3)
            suffix = m.group(4)
            home = str(Path.home())
            if op == "%":
                # Shortest suffix match (longest kept) vs longest match.
                rng = (range(len(home), -1, -1) if not double
                       else range(0, len(home) + 1))
                for k in rng:
                    if fnmatch.fnmatchcase(home[k:], pat):
                        t = home[:k] + suffix
                        break
                else:
                    t = home + suffix
            else:  # "#" prefix removal
                rng = (range(0, len(home) + 1) if not double
                       else range(len(home), -1, -1))
                for k in rng:
                    if fnmatch.fnmatchcase(home[:k], pat):
                        t = home[k:] + suffix
                        break
                else:
                    t = home + suffix
            if not t.startswith("/"):
                return t
            norm = posixpath.normpath(t)
            return "/" if norm == "//" else norm
        # ${HOME<op>word} parameter expansion; HOME is set in practice.
        rest = t[len("${HOME"):]
        for op in (":-", "-", ":+", "+", ":=", "=", ":?", "?"):
            if not rest.startswith(op):
                continue
            end = rest.find("}", len(op))
            if end == -1:
                break
            word = rest[len(op):end]
            suffix = rest[end + 1:]
            if op in (":-", "-", ":=", "=", ":?", "?"):
                t = str(Path.home()) + suffix
            else:  # :+ / + : the word itself, when HOME is set
                t = word + suffix
            break
        else:
            m = re.match(r"^\$\{HOME\}", t)
            if m:
                t = str(Path.home()) + t[m.end():]
    elif t.startswith("~"):
        m = re.match(r"^~([^/]*)(/.*)?$", t)
        if m:
            name, rest = m.group(1), m.group(2) or ""
            if name == "":
                t = str(Path.home()) + rest
            else:
                try:
                    import pwd

                    t = pwd.getpwnam(name).pw_dir + rest
                except Exception:  # noqa: BLE001 - unknown user: left literal
                    pass
    else:
        m = re.match(r"^\$HOME", t)
        if m:
            t = str(Path.home()) + t[m.end():]
    if not t.startswith("/"):
        return t
    norm = posixpath.normpath(t)
    return "/" if norm == "//" else norm


def _home_dirs() -> set[str]:
    """Every account home directory on this machine, plus $HOME.

    The blocklist protects "the home directory", and the shell can spell
    *any* account's home via ``~user`` — so the protected set is every
    passwd entry's home, not just the current user's. Unknown on
    non-POSIX platforms: falls back to $HOME alone.
    """
    homes = {str(Path.home())}
    try:
        import pwd

        homes.update(entry.pw_dir for entry in pwd.getpwall())
    except Exception:  # noqa: BLE001 - pwd missing or unreadable: $HOME only
        pass
    return homes


def _expand_braces(tok: str) -> list[str]:
    """Expand ``{a,b}`` brace groups the way the shell does, before globbing.

    Brace expansion runs before pathname expansion, so ``/{,}`` is ``/``
    (twice) and ``/home/{hatch,shared}`` includes ``/home/hatch`` — both
    slipped past the old check, which saw only the literal braces. Groups
    without a comma (``{a}``) are literal in the shell and left alone,
    as are escaped (``\\{``) and nested groups. Expansion is capped at
    256 combinations: beyond that the token is returned unexpanded
    rather than exploding.

    Sequence groups (``{a..z}``, ``{1..10}``, with an optional
    ``..step``) expand the same way, and slipped past the old check
    for the same reason: ``/hom{e..f}`` is ``/home`` (plus ``/homf``)
    once the shell expands it, and ``/home/hat{c..d}h`` includes a
    home dir. A zero step means step 1 in the shell and is treated
    that way here; numbers are zero-padded when either endpoint has
    a leading zero, as the shell pads them.
    """
    out = [tok]
    while True:
        expanded: list[str] = []
        changed = False
        for s in out:
            start = -1
            i = 0
            while i < len(s):
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "{":
                    start = i
                    break
                i += 1
            if start == -1:
                expanded.append(s)
                continue
            depth = 0
            end = -1
            for j in range(start, len(s)):
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end == -1 or depth != 0:
                expanded.append(s)
                continue
            inner = s[start + 1:end]
            if "{" in inner:
                expanded.append(s)
                continue
            if "," not in inner:
                seq = re.match(
                    r"^(-?[a-zA-Z]|-?\d+)\.\.(-?[a-zA-Z]|-?\d+)"
                    r"(?:\.\.(-?\d+))?$", inner)
                alts: list[str] | None = None
                if seq and ((len(seq.group(1)) == 1
                             and len(seq.group(2)) == 1
                             and seq.group(1).isalpha()
                             and seq.group(2).isalpha())
                            or (seq.group(1).lstrip("-").isdigit()
                                and seq.group(2).lstrip("-").isdigit())):
                    start_v, end_v = seq.group(1), seq.group(2)
                    step = abs(int(seq.group(3))) if seq.group(3) else 1
                    step = step or 1  # a zero step means step 1
                    if start_v.isalpha():
                        lo = (range(ord(start_v), ord(end_v) + 1, step)
                              if ord(start_v) <= ord(end_v)
                              else range(ord(start_v), ord(end_v) - 1,
                                         -step))
                        alts = [chr(c) for c in lo]
                    else:
                        a, b = int(start_v), int(end_v)
                        width = 0
                        if ((len(start_v.lstrip("-")) > 1
                             and start_v.lstrip("-").startswith("0"))
                                or (len(end_v.lstrip("-")) > 1
                                    and end_v.lstrip("-").startswith("0"))):
                            width = max(len(start_v.lstrip("-")),
                                        len(end_v.lstrip("-")))
                        rng = (range(a, b + 1, step) if a <= b
                               else range(a, b - 1, -step))
                        if width:
                            alts = [("-" + str(abs(n)).zfill(width)
                                     if n < 0 else str(n).zfill(width))
                                    for n in rng]
                        else:
                            alts = [str(n) for n in rng]
                if alts is None:
                    expanded.append(s)
                    continue
                changed = True
                for alt in alts:
                    expanded.append(s[:start] + alt + s[end + 1:])
                continue
            changed = True
            for alt in inner.split(","):
                expanded.append(s[:start] + alt + s[end + 1:])
        out = expanded
        if not changed:
            return out
        if len(out) > 256:
            return [tok]


def _has_glob(seg: str) -> bool:
    return any(c in seg for c in "*?[")


def _is_broad_glob(seg: str) -> bool:
    """True if *seg* matches (essentially) every non-dot entry.

    The shell's glob never matches dotfiles, so ``*`` wipes "every
    entry" in exactly this sense — and ``?*`` / ``*?`` / ``*?*`` /
    ``[!.]*`` match the same set: any name of length >= 1 not
    starting with ``.``. A broad glob directly under a guarded dir
    therefore wipes that dir just as ``*`` does (``$HOME/?*`` slipped
    past the old check, which only understood bare ``*`` chains).
    Narrower patterns (``[a-z]*``, ``*.pyc``, ``?``) match only a
    subset and stay the caller's responsibility, like the allowed
    ``$HOME/project/*``.
    """
    if seg.startswith("[!.]") or seg.startswith("[^.]"):
        seg = seg[4:]
    if not seg or any(c not in "*?" for c in seg):
        return False
    return "*" in seg and seg.count("?") <= 1


def _guarded_dirs() -> set[str]:
    """``/``, every account home, and the directory holding the homes."""
    protected = {"/"} | _home_dirs()
    parents = {
        posixpath.dirname(home)
        for home in protected
        if home != "/" and posixpath.dirname(home) not in ("", "/")
    }
    return protected | parents


def _glob_hits_guarded(spell: str) -> bool:
    """True if a glob pattern in *spell* can delete a guarded dir.

    The shell expands globs before acting, so a pattern that *can*
    match a guarded dir is a wipe of it even though the literal
    spelling names no guarded dir: ``/hom?`` matches ``/home``,
    ``/h*`` matches ``/home``, and ``/home/?*`` / ``/home/[a-z]*``
    match every home inside ``/home`` — all three slipped past the
    old check, which only understood bare ``*`` chains. The walk
    tracks only guarded-relevant expansions (a pattern under a
    non-guarded dir can only become dangerous by matching a guarded
    dir's name), so precision is preserved:

    - A glob segment under a guarded dir followed only by further
      globs wipes that dir's contents (``/home/*/*``, ``/*/*``) —
      blocked. So does a *broad* glob directly under a guarded dir
      (``$HOME/?*`` matches every non-dot home entry, exactly like
      ``$HOME/*``) — while a trailing glob that matches a guarded
      dir name there (``/home/[a-z]*`` matches every home) is
      blocked for deleting the guarded dirs themselves. A pattern
      matching no guarded name and no broad set (``/*.pyc``) stays
      allowed.
    - A glob segment under a guarded dir followed by a *named*
      segment deletes a named subpath, not the dir (``/home/*/project``
      is the glob analogue of the allowed ``$HOME/project/*``) — the
      walk only continues if the pattern matches a guarded dir name
      and the named tail lands on one (``/hom?/hatch`` blocked,
      ``/hom?/shared`` allowed).
    - A glob under a non-guarded dir that matches no guarded dir name
      (``/tmp/*``, ``/home/user/project/*``) stays allowed.

    Non-absolute tokens are returned untouched: a relative path can
    never be ``/`` or the home directory.
    """
    if not spell.startswith("/"):
        return False
    guarded = _guarded_dirs()
    segments = [s for s in spell.split("/") if s != ""]
    current = {"/"}
    for i, seg in enumerate(segments):
        rest = segments[i + 1:]
        if not _has_glob(seg):
            current = {
                posixpath.join(d, seg) if d != "/" else "/" + seg
                for d in current
            }
            continue
        nxt: set[str] = set()
        for d in current:
            if d in guarded and rest and all(_has_glob(s) for s in rest):
                return True
            if d in guarded and not rest and _is_broad_glob(seg):
                return True
            for g in guarded:
                if posixpath.dirname(g) == d and fnmatch.fnmatchcase(
                        posixpath.basename(g), seg):
                    nxt.add(g)
        current = nxt
        if not current:
            return False
    return any(d in guarded for d in current)


def _is_root_or_home(tok: str) -> bool:
    """True for ``/``, any account home — and their wipe forms.

    Path spellings are normalized first, so obfuscated roots like
    ``rm -rf /./`` or ``rm -rf /../`` (which the shell resolves to ``/``)
    cannot slip past the literal ``/`` check, and ``~user`` / ``${HOME}``
    parameter-expansion spellings of a home dir are caught too.

    Two spellings the old check missed, both verified against
    ``_is_dangerous`` before this fix:

    - The directory *containing* the homes. ``rm -rf /home`` (spelled
      directly, or as ``$HOME/..`` / ``~/..``) deletes every home inside
      it, so the parent of a home counts as guarded — unlike a sibling
      such as ``/home/shared``, which stays allowed. This also
      generalizes the already-blocked ``~root/..`` (whose parent is
      ``/``) to ordinary homes, whose parent is ``/home``.
    - Pure-star chains under a guarded dir. ``/home/*`` expands to all
      homes at once, ``/*/*`` to the contents of every top-level dir,
      and ``$HOME/*/*`` to the contents of every home subdir — each is
      a wipe of a guarded dir just as ``/*`` is a wipe of ``/``. Only
      chains of bare ``*`` segments count: ``$HOME/project/*`` (a star
      under a *named* subdir) stays allowed.
    - Glob and brace spellings of a guarded dir. ``/hom?`` / ``/h*``
      expand to ``/home``, ``/home/?*`` / ``/home/[a-z]*`` expand to
      every home, and ``/{,}`` / ``/home/{,}`` brace-expand to ``/`` /
      ``/home`` — all verified against ``_is_dangerous`` before this
      fix. See ``_glob_hits_guarded`` / ``_expand_braces`` for the
      precision rules (a glob under a named subdir stays allowed).
    - Brace-sequence and ``${HOME}`` substring / suffix-removal
      spellings of a guarded dir. ``/hom{e..f}`` brace-expands to
      ``/home``, ``${HOME:0:5}`` / ``${HOME:0}`` substring-expand to
      the home container / home itself, ``${HOME%/*}`` removes to
      ``/home``, and ``${HOME: -11}`` (space form, one word in the
      shell) substring-expands to home itself — all verified against
      ``_is_dangerous`` before this fix.
    - ANSI-C-quoted spellings of a guarded dir. ``$'/home'``,
      ``$'\\x2fhome'`` (hex escape), ``$'\\057home'`` (octal escape),
      ``/$'home'`` and ``$'/'home`` all expand to a guarded dir, and
      ``/home/$'..'`` expands to ``/`` — all verified against
      ``_is_dangerous`` before this fix, which saw only shlex-mangled
      tokens like ``$/home``. See ``_expand_ansi_c_quoting``.
    """
    t = tok.strip("\"'")
    if not t:
        return False
    guarded = _guarded_dirs()
    for expansion in _expand_braces(t):
        spell = _normalize_path_spelling(expansion)
        if not spell:
            # An expansion to empty (e.g. ${HOME%%/*}) is no argument
            # at all in the shell — not "/", which the rstrip fallback
            # below would otherwise turn it into.
            continue
        base = spell.rstrip("/") or "/"
        if base in guarded:
            return True
        # Strip trailing /* segments: if what remains is a guarded dir, the
        # glob wipes it (/*, /*/*, /home/*, /home/*/*, $HOME/*/*).
        stripped = base
        while stripped.endswith("/*"):
            stripped = stripped[:-2] or "/"
        if stripped in guarded:
            return True
        if _glob_hits_guarded(spell):
            return True
    return False


def _args_until_separator(tokens: list[str], start: int) -> list[str]:
    """Tokens from *start* up to (not including) the next shell separator."""
    out: list[str] = []
    for tok in tokens[start:]:
        if tok in _SEPARATORS:
            break
        out.append(tok)
    return out


def _rm_reason(tokens: list[str]) -> str | None:
    """Recursive rm aimed at / or the home dir — however rm is spelled.

    Matches ``rm`` by basename so ``/bin/rm`` and ``sudo /bin/rm`` are
    caught too; precise about targets so `rm -rf ./build` passes.
    """
    for i, tok in enumerate(tokens):
        if _basename(tok) != "rm":
            continue
        args = _args_until_separator(tokens, i + 1)
        recursive = any(
            t == "--recursive"
            or (t.startswith("-") and not t.startswith("--")
                and "r" in t[1:])
            for t in args)
        targets = [t for t in args if not t.startswith("-")]
        if recursive and any(_is_root_or_home(t) for t in targets):
            return "recursive delete of / or home directory"
    return None


def _find_reason(tokens: list[str]) -> str | None:
    """``find / -delete`` (or -exec over / or home) wipes by search root."""
    for i, tok in enumerate(tokens):
        if _basename(tok) != "find":
            continue
        args = _args_until_separator(tokens, i + 1)
        if not any(t in ("-delete", "-exec", "-execdir") for t in args):
            continue
        paths = [t for t in args if not t.startswith("-")]
        if any(_is_root_or_home(p) for p in paths):
            return "find deleting from / or home directory"
    return None


def _nested_reason(tokens: list[str], depth: int) -> str | None:
    """Rescan payloads handed to a nested shell (``bash -c '...'``, eval)."""
    if depth >= 3:
        return None
    for i, tok in enumerate(tokens):
        if _basename(tok) in _SHELLS and i + 2 < len(tokens) \
                and tokens[i + 1] == "-c":
            reason = _is_dangerous(tokens[i + 2], depth + 1)
            if reason:
                return reason
        if tok == "eval" and i + 1 < len(tokens):
            payload = " ".join(_args_until_separator(tokens, i + 1))
            reason = _is_dangerous(payload, depth + 1)
            if reason:
                return reason
    return None


# ANSI-C quoting: the shell decodes $'...' escapes before acting, but
# shlex strips the quotes first and leaves "$/home" / "$\\x2fhome" — tokens
# the path normalizers below never recognize. So $'...' / $"..." segments
# are decoded on the raw command, before tokenizing (see
# _expand_ansi_c_quoting). Unknown escapes keep their backslash, exactly
# as the shell leaves them ($'\\q' is "\\q", not "q").
_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _decode_ansi_c(body: str) -> str:
    """Decode the inside of a ``$'...'`` segment the way the shell does."""
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(c)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "\n":  # line continuation: both characters vanish
            i += 2
            continue
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
            continue
        if nxt == "x":
            j = i + 2
            hexs = ""
            while (j < len(body) and len(hexs) < 2
                   and body[j] in _HEX_DIGITS):
                hexs += body[j]
                j += 1
            if hexs:
                out.append(chr(int(hexs, 16)))
                i = j
            else:
                out.append("\\x")
                i += 2
            continue
        if nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            hexs = body[i + 2:i + 2 + width]
            if (len(hexs) == width
                    and all(ch in _HEX_DIGITS for ch in hexs)):
                out.append(chr(int(hexs, 16)))
                i += 2 + width
            else:
                out.append("\\" + nxt)
                i += 2
            continue
        if nxt == "c" and i + 2 < len(body) and body[i + 2].isalpha():
            out.append(chr(ord(body[i + 2].upper()) - 64))
            i += 3
            continue
        if nxt in "01234567":
            j = i + 1
            octs = ""
            while (j < len(body) and len(octs) < 3
                   and body[j] in "01234567"):
                octs += body[j]
                j += 1
            out.append(chr(int(octs, 8) & 0xFF))
            i = j
            continue
        out.append("\\" + nxt)  # unknown escape: backslash kept, as in bash
        i += 2
    return "".join(out)


def _expand_ansi_c_quoting(command: str) -> str:
    """Replace unquoted ``$'...'`` / ``$"..."`` segments with their value.

    ``rm -rf $'/home'`` / ``$'\\x2fhome'`` / ``/$'home'`` all delete a
    guarded dir once the shell expands them, but the old scan saw only
    the shlex-mangled ``$/home`` / ``$\\x2fhome`` / ``/$home`` and let
    them through (verified against ``_is_dangerous`` before this fix).
    Each decoded segment is re-wrapped in single quotes so it stays one
    word for shlex, exactly as the shell keeps it one word; the normal
    path checks then see the real path. Segments inside single or double
    quotes are left alone: there the shell does not expand them either
    (``'$'/home''``-style nesting still reaches the nested-shell rescan
    as a proper ``$'...'`` payload, so it is caught on the second pass).
    Decoded text flows through the same quote-insensitive normalizers
    as the rest of the scan, matching the existing treatment of quoted
    expansions such as ``'${HOME}'``.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j == -1:
                out.append(command[i:])
                break
            out.append(command[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            # Inside double quotes the shell strips the backslash from
            # \$ but shlex does not — so a nested payload written as
            # bash -c "rm -rf \$'/home'" reaches the nested-shell rescan
            # as rm -rf \$'/home', which shlex mangles to $/home and the
            # ANSI-C decode above never sees. Translating \$' -> $'
            # inside double quotes hands the rescan the payload the
            # inner shell actually receives. Only \$' is translated:
            # \$VAR forms already resolve through the rescan, and
            # translating \$" would break shlex quote structure.
            j = i + 1
            seg: list[str] = []
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    if (command[j + 1] == "$" and j + 2 < n
                            and command[j + 2] == "'"):
                        seg.append("$'")
                        j += 3
                        continue
                    seg.append(command[j:j + 2])
                    j += 2
                    continue
                if command[j] == '"':
                    break
                seg.append(command[j])
                j += 1
            out.append('"' + "".join(seg) + ('"' if j < n else ""))
            i = j + 1 if j < n else n
            continue
        if c == "$" and i + 1 < n and command[i + 1] in "'\"":
            quote = command[i + 1]
            j = i + 2
            body: list[str] = []
            closed = False
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    body.append(command[j:j + 2])
                    j += 2
                    continue
                if command[j] == quote:
                    closed = True
                    break
                body.append(command[j])
                j += 1
            if not closed:
                out.append(command[i:])
                break
            raw = "".join(body)
            if quote == "'":
                decoded = _decode_ansi_c(raw)
            else:  # $"...": locale string, content is literal
                decoded = raw.replace('\\"', '"').replace("\\\\", "\\")
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _is_dangerous(command: str, _depth: int = 0) -> str | None:
    """Blocklist scan. Returns a reason string, or None if the command passes.

    This is a blocklist, not a sandbox: it stops the obvious footguns, not a
    determined adversary. The permission gate is the real protection, and
    real isolation belongs in a container layer. The scan is spelling-aware
    (``/bin/rm`` counts as ``rm``), covers ``$HOME``/``~`` targets and
    ``$'...'`` ANSI-C-quoted spellings of them, recurses into nested
    ``sh -c``/``eval`` payloads, and catches ``find / -delete`` and
    download-piped-to-shell one-liners.
    """
    command = _expand_ansi_c_quoting(command)
    patterns = [
        (r":\(\)\s*\{", "fork bomb"),
        (r"\bmkfs(\.\w+)?\b", "filesystem format"),
        (r"\bdd\b[^\n|;&]*\bof=/dev/", "raw disk write"),
        (r">\s*/dev/(sd|hd|nvme|vd)[a-z0-9]*", "raw disk write"),
        (r"\b(sha256sum\s+)?>\s*/dev/(mem|kmem|port)\b", "raw device write"),
        (r"\b(shutdown|reboot|halt|poweroff)\b", "power control"),
        # The permission-nuke pattern used to require trailing whitespace
        # after "/": the plain "chmod -R 777 /" (end of command, or "/;"
        # before the next command) slipped past it. The terminator is now
        # whitespace, end of command, or a shell separator, and any flag
        # spellings (-R, -Rf, --recursive) are accepted before the mode,
        # so only the root dir itself is guarded — /tmp or /home stay
        # allowed, as before.
        (r"\bchmod\s+(?:-\S+\s+)*0?777\s+/+(?:\s|$|[;&|])",
         "permission nuke"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, command):
            return reason
    if _PIPE_TO_SHELL.search(command):
        return "download piped to a shell"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None  # unparseable; the shell will report the syntax error
    # Spaces inside ${...} are part of the expansion, not word
    # separators: the shell reads ${HOME: -11} as one word (the whole
    # home dir), but shlex splits it into "${HOME:" and "-11}".
    # Rejoin tokens while a ${...} group is still open so the
    # normalizers above see the full expansion form.
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.count("${") > tok.count("}") and i + 1 < len(tokens):
            parts = [tok]
            depth = tok.count("${") - tok.count("}")
            j = i + 1
            while j < len(tokens) and depth > 0:
                parts.append(tokens[j])
                depth += tokens[j].count("${") - tokens[j].count("}")
                j += 1
            merged.append(" ".join(parts))
            i = j
        else:
            merged.append(tok)
            i += 1
    tokens = merged
    return (_rm_reason(tokens) or _find_reason(tokens)
            or _nested_reason(tokens, _depth))


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


def _assert_public_host(host: str) -> None:
    """Refuse loopback/private/link-local/etc. hosts (SSRF guard).

    Resolves ``host`` and raises :exc:`ValueError` when any resolved
    address is non-public, or when the host does not resolve at all.
    Shared by the ``fetch_url`` tool and the browser engine so both
    enforce the same boundary.
    """
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
    _assert_public_host(parsed.hostname)
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
            "The working directory is confined to the allowed roots, but "
            "the command itself is NOT sandboxed: a destructive-command "
            "blocklist and the permission gate are the protections."
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
