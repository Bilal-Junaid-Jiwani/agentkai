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
_SEPARATORS = {";", "&&", "||", "|", "&", "`"}
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


# Long options that take a separate value word (see _shell_c_payload).
_VALUE_OPTIONS = {"--init-file", "--rcfile"}


def _shell_c_payload(tokens: list[str], start: int) -> str | None:
    """The ``-c`` command string of a shell invocation, or None.

    ``start`` points just past the shell word. The old scan only
    recognized a standalone ``-c`` word immediately after the shell,
    but the shell's option parser is far more forgiving, and every
    one of these runs the payload (verified against ``_is_dangerous``
    before this fix):

    - an attached argument: ``sh -c'rm -rf /'`` — shlex merges it
      into the single token ``-crm -rf /``;
    - options before ``-c``: ``bash --norc -c '...'``,
      ``bash -o posix -c '...'``, ``bash --rcfile foo -c '...'``;
    - short-option clusters: ``sh -ec '...'`` (``-e`` then ``-c``),
      ``sh -xc'...'`` (``-x`` then ``-c`` with an attached argument).

    The walk mirrors getopt: long options (``--opt=val`` as one word,
    known value-taking options consuming the next word), short-option
    clusters scanned left to right (``-o`` consumes the rest of its
    cluster, or the next word, as its value; ``-c`` takes the rest of
    its cluster, or the following words, as the command). ``--`` ends
    option parsing and a first non-option word is a script file, not
    a payload, so ``bash script.sh -c '...'`` and ``sh -- -c '...'``
    stay untouched.
    """
    j = start
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":
            return None
        if tok.startswith("--"):
            if "=" in tok:
                j += 1
            elif tok in _VALUE_OPTIONS:
                j += 2
            else:
                j += 1
            continue
        if tok.startswith("+") and len(tok) > 1:
            j += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            k = 1
            consumed_value = False
            while k < len(tok):
                ch = tok[k]
                if ch == "c":
                    rest = tok[k + 1:]
                    if rest:
                        return " ".join(
                            [rest] + _args_until_separator(tokens, j + 1))
                    if j + 1 < len(tokens):
                        return " ".join(
                            _args_until_separator(tokens, j + 1))
                    return None
                if ch == "o":
                    # -o's argument is the rest of the cluster, if any,
                    # else the next word.
                    consumed_value = k + 1 >= len(tok)
                    break
                k += 1
            j += 2 if consumed_value else 1
            continue
        return None
    return None


def _nested_reason(tokens: list[str], depth: int) -> str | None:
    """Rescan payloads handed to a nested shell (``bash -c '...'``, eval).

    The ``-c`` payload is found by ``_shell_c_payload``, which
    understands attached arguments, clusters and preceding options —
    not just a standalone ``-c`` word. The payload is rejoined up to
    the next top-level separator, since operator splitting (see
    ``_split_shell_operators``) may have broken a grouped payload such
    as ``'(rm -rf /)'`` into several tokens.
    """
    if depth >= 3:
        return None
    for i, tok in enumerate(tokens):
        if _basename(tok) in _SHELLS:
            payload = _shell_c_payload(tokens, i + 1)
            if payload:
                reason = _is_dangerous(payload, depth + 1)
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


_SYMBOLIC_CLAUSE = re.compile(r"^([ugoa]*)([+-=])([rwxXstugo]*)$")


def _chmod_symbolic_nukes_root(mode: str) -> bool:
    """True if symbolic *mode* leaves ``/`` world-writable.

    The numeric pattern above catches ``777`` in any octal spelling,
    but the symbolic spellings of the same mode slipped past it:
    ``chmod a+rwx /``, ``a=rwx /`` and ``u=rwx,g=rwx,o=rwx /`` all
    make root world-writable, as do ``go+rwx /`` (root's owner already
    has full access) and plain ``o+w /``. The rule is the end state,
    not any single clause: clauses apply in order (``a+rwx,o-w /``
    takes the write bit back off other, so it stays allowed), ``=``
    replaces a class's bits, and ``-`` can only remove. Copy letters
    (``o=u``) grant the source class's usual bits on ``/`` — owner
    root has rwx, group root has r-x. An empty who (``+rwx``) is
    umask-masked in the shell, so with the standard 022 umask other
    gains r-x but not w, and it stays allowed, exactly like the
    allowed ``755 /``. ``s``/``t`` grant no write and are ignored;
    ``X`` on a directory behaves as ``x``. A non-symbolic mode
    (``777``, ``--help``) returns False: the numeric pattern owns it.
    """
    perms: dict[str, set[str]] = {"u": set(), "g": set(), "o": set()}
    for clause in mode.split(","):
        m = _SYMBOLIC_CLAUSE.match(clause)
        if not m:
            return False
        who, op, letters = m.groups()
        if "a" in who:
            classes = {"u", "g", "o"}
        elif who:
            classes = set(who)
        else:
            classes = {"u", "g", "o"}
        masked = not who  # empty who: umask 022 strips w from g/o
        granted: set[str] = set()
        for ch in letters:
            if ch in "rwx":
                granted.add(ch)
            elif ch == "X":
                granted.add("x")  # target is a dir, so X behaves as x
            elif ch == "u":
                granted.update("rwx")
            elif ch in "go":
                granted.update("rx")
            # "s"/"t": setuid/setgid/sticky — no write grant, ignored
        for c in classes:
            grant = set(granted)
            if masked and c in "go":
                grant.discard("w")
            if op == "=":
                perms[c] = grant
            elif op == "+":
                perms[c] |= grant
            else:  # "-": removal can only take write away
                perms[c] -= grant
    return "w" in perms["o"]


def _chmod_reason(tokens: list[str]) -> str | None:
    """Symbolic chmod that leaves ``/`` world-writable (see above).

    Scoped exactly like the numeric pattern: only the root dir itself
    is guarded, so ``a+rwx /tmp`` and ``a+rwx /home`` stay allowed.
    ``chmod`` is matched by basename (``/bin/chmod``, ``sudo chmod``)
    like ``_rm_reason``; ``--reference <file>`` is skipped as a
    flag-with-argument, and ``--`` ends flag parsing. A target token
    with an attached separator (``/;`` from shlex, which does not split
    ``;``) still counts as root.
    """
    for i, tok in enumerate(tokens):
        if _basename(tok) != "chmod":
            continue
        args = _args_until_separator(tokens, i + 1)
        j = 0
        while j < len(args):
            arg = args[j]
            if arg == "--":
                j += 1
                break
            if not arg.startswith("-"):
                break
            if arg == "--reference":
                j += 2
                continue
            j += 1
        if j >= len(args):
            continue
        mode = args[j]
        if not _chmod_symbolic_nukes_root(mode):
            continue
        for target in args[j + 1:]:
            if target.startswith("-"):
                continue
            root = target.strip("\"'").rstrip(";&|").rstrip("/")
            if target and root == "":
                return "permission nuke"
    return None


# Shell operators shlex never splits on (see _split_shell_operators).
# Backtick is included: `` `rm -rf /` `` tokenizes with `` `rm`` and
# ``/` `` as words, which neither the command nor the path checks
# recognize, so backtick command substitution slipped past the scan
# (verified against ``_is_dangerous`` before this fix). Splitting it
# makes the substituted command an ordinary token sequence, and the
# backtick is a separator too, so a following word is not mistaken
# for the substituted command's argument.
_SHELL_OPERATORS = re.compile(r"([;&|()`]+)")


def _split_shell_operators(tokens: list[str]) -> list[str]:
    """Split shell operators glued to neighbouring words.

    ``shlex`` splits on whitespace only, so it never separates the
    shell's own operators: ``rm -rf /; echo`` tokenizes with ``/;``
    as one word, ``(rm -rf /)`` with ``(rm`` and ``/)``, and
    ``echo hi;rm -rf /`` with ``hi;rm`` — none of which the command
    or path checks recognize, so all three slipped past the scan
    (verified against ``_is_dangerous`` before this fix). The shell
    itself splits on these operators whether or not whitespace
    surrounds them, so the scan splits each token on runs of
    ``; & | ( ) ` `` and keeps the operators as their own tokens, which
    ``_args_until_separator`` already treats as command boundaries.
    Quoted operators lose their quoting here (shlex already dropped
    it), so a literal ``;`` inside a quoted guarded path is an
    accepted imprecision — the same quote-blindness the path checks
    already have.
    """
    out: list[str] = []
    for tok in tokens:
        out.extend(part for part in _SHELL_OPERATORS.split(tok) if part)
    return out


# A raw disk / memory device as a write destination: the same write
# the ``dd of=`` and ``>`` redirect patterns above already block, spelled
# as an ordinary file copy. ``cp /dev/zero /dev/sda`` overwrites the disk
# exactly like ``dd if=/dev/zero of=/dev/sda``, and ``tee /dev/sda``
# writes its stdin there — but neither matched any pattern or token
# check, since the scan only knew ``dd``/redirect spellings of a raw
# device write (verified against ``_is_dangerous`` before this fix).
# Only the destination counts: reading a device (``cp /dev/sda
# /tmp/backup``) or writing to a harmless device (``/dev/null``,
# ``/dev/zero``, ``/dev/full``) stays allowed, and a bare ``/dev``
# directory is not a device file. ``cp``/``mv`` take their last operand
# as the destination unless ``-t``/``--target-directory`` names it
# (attached ``-tDIR`` and clustered ``-rt DIR`` spellings included);
# ``tee`` writes to every operand. The same write spelled through a
# device-mapper node (``/dev/dm-0``, ``/dev/mapper/vg-lv``), a loop
# device (``/dev/loop0``), a RAID array (``/dev/md0``), a ramdisk
# (``/dev/ram0``), or a network block device (``/dev/nbd0``,
# ``/dev/rbd0``) destroys the same data exactly like the canonical
# spelling — but the literal match above only knew ``sd``/``hd``/``vd``,
# ``nvme``, ``mmcblk``, and ``mem``/``kmem``/``port``, so all of those
# slipped past the copy and wipe checks (verified against
# ``_is_dangerous`` before this fix). The stable symlinks under
# ``/dev/disk/by-*/`` and the mapper nodes are covered separately in
# ``_is_raw_device_spelling``: their names are arbitrary, so no fixed
# name list can enumerate them.
_RAW_DEVICE = re.compile(
    r"^/dev/(?:(?:sd|hd|vd)[a-z]+[0-9]*"
    r"|nvme[0-9]+n[0-9]+(?:p[0-9]+)?"
    r"|mmcblk[0-9]+(?:p[0-9]+)?"
    r"|dm-[0-9]+"
    r"|loop[0-9]+(?:p[0-9]+)?"
    r"|md[0-9]+"
    r"|ram[0-9]+"
    r"|nbd[0-9]+(?:p[0-9]+)?"
    r"|rbd[0-9]+(?:p[0-9]+)?"
    r"|mem|kmem|port)$")


# The ``/dev/disk/`` subdirectories that hold only stable symlinks to
# block devices: any entry under one names a disk or a partition of
# one, whatever its (arbitrary, vendor-chosen) name is.
_DISK_BY_KINDS = ("by-id", "by-path", "by-partuuid", "by-label",
                  "by-uuid", "by-partlabel")


# Long options that take a separate value word, per command, for the
# raw-device checks below. ``install`` copies like ``cp`` (including
# ``-t``/``--target-directory``), but also takes ``--mode``/``--owner``/
# ``--group``/``--strip-program`` values that must not be mistaken for
# operands: ``install --mode 644 /dev/zero /dev/sda`` still writes the
# device, and ``install --owner root /dev/zero /tmp/x`` still does not.
_INSTALL_VALUE_OPTIONS = {"--mode", "--owner", "--group", "--strip-program"}


def _normalize_device_spelling(tok: str) -> str:
    """Collapse dot segments / repeated separators in a device spelling.

    The shell resolves ``//``, ``/./`` and ``/../`` before acting, so
    ``/dev/./sda`` and ``//dev/sda`` name ``/dev/sda`` exactly like the
    canonical spelling — but the ``_RAW_DEVICE`` match above is literal,
    so both slipped past the copy and wipe checks. Reuse
    ``_normalize_path_spelling`` (which already collapses those segments
    for the ``/``/home checks) and collapse a leading ``//`` too, which
    POSIX leaves implementation-defined and ``posixpath.normpath``
    therefore preserves verbatim.
    """
    spell = _normalize_path_spelling(tok.strip("\"'"))
    while spell.startswith("//"):
        spell = spell[1:]
    return spell


_RAW_DEVICE_NAMES: list[str] | None = None


def _raw_device_names() -> list[str]:
    """Representative raw-device names, for glob-spelling checks.

    The names ``_RAW_DEVICE`` matches, enumerated: ``mem`` / ``kmem`` /
    ``port``; ``sd`` / ``hd`` / ``vd`` with one or two letters and an
    optional partition (1-15); ``nvme{0..3}n{0..3}`` with an optional
    ``p`` partition (1-3); ``mmcblk{0..3}`` likewise; ``dm-{0..3}``;
    ``loop{0..7}`` with an optional ``p`` partition (1-3); ``md{0..3}``;
    ``ram{0..3}``; ``nbd{0..3}`` / ``rbd{0..3}`` with an optional ``p``
    partition (1-3). Built lazily and cached: a glob pattern is only
    ever matched against this list, never against the live ``/dev`` —
    the check must be deterministic in tests and must not depend on
    which devices this machine happens to have.
    """
    global _RAW_DEVICE_NAMES
    if _RAW_DEVICE_NAMES is None:
        names = ["mem", "kmem", "port"]
        letters = [chr(c) for c in range(ord("a"), ord("z") + 1)]
        bases = letters + [a + b for a in letters for b in letters]
        for prefix in ("sd", "hd", "vd"):
            for base in bases:
                names.append(prefix + base)
                for part in range(1, 16):
                    names.append(f"{prefix}{base}{part}")
        for ctl in range(4):
            for ns in range(4):
                names.append(f"nvme{ctl}n{ns}")
                for part in range(1, 4):
                    names.append(f"nvme{ctl}n{ns}p{part}")
        for disk in range(4):
            names.append(f"mmcblk{disk}")
            for part in range(1, 4):
                names.append(f"mmcblk{disk}p{part}")
        for i in range(4):
            names.append(f"dm-{i}")
            names.append(f"md{i}")
            names.append(f"ram{i}")
            names.append(f"nbd{i}")
            names.append(f"rbd{i}")
            for part in range(1, 4):
                names.append(f"nbd{i}p{part}")
                names.append(f"rbd{i}p{part}")
        for i in range(8):
            names.append(f"loop{i}")
            for part in range(1, 4):
                names.append(f"loop{i}p{part}")
        _RAW_DEVICE_NAMES = names
    return _RAW_DEVICE_NAMES


def _is_raw_device_spelling(tok: str) -> bool:
    """True if *tok* names a raw device after shell expansion.

    Brace groups expand first (``/dev/{sda,sdb}`` is two words, and
    ``tee`` / ``shred`` take every word, so each expansion counts),
    then each spelling is checked literally — and a glob spelling is
    checked segment-wise against the representative device names,
    since the shell expands ``/dev/sd[a]``, ``/dev/sd?`` and
    ``/dev/nvme[0-9]n1`` to the device before acting, while the
    literal ``_RAW_DEVICE`` match never saw them (verified against
    ``_is_dangerous`` before this fix). A glob is only dangerous when
    it can match a device path: the pattern must have exactly the
    ``/dev/<name>`` shape — a pattern under another directory, or
    with more segments, cannot name one (``/tmp/*`` and
    ``/dev/pts/*`` stay allowed) — and each segment must fnmatch,
    with ``*`` never crossing a ``/`` the way the shell's glob works,
    so ``/*`` stays allowed. Quoted braces expand the same way
    ``_is_root_or_home`` already expands them. Stable symlinks under
    ``/dev/disk/by-*/`` and device-mapper nodes under ``/dev/mapper/``
    are the exception to the two-segment shape: their names are
    arbitrary (``ata-WDC_…``, ``vg-lv``), so no name list can cover
    them — but those directories hold only device nodes, so any entry
    under one names a disk or a partition of one, literal, brace, or
    glob (a glob there can only expand to device symlinks). The
    directories themselves (``/dev/disk/by-id``, ``/dev/mapper``) are
    not devices.
    """
    for expansion in _expand_braces(tok):
        spell = _normalize_device_spelling(expansion)
        if not spell:
            continue
        if _RAW_DEVICE.match(spell):
            return True
        segments = [s for s in spell.split("/") if s != ""]
        if len(segments) == 2 and fnmatch.fnmatchcase("dev", segments[0]):
            name = segments[1]
            if _has_glob(name):
                if any(fnmatch.fnmatchcase(n, name)
                       for n in _raw_device_names()):
                    return True
            elif _RAW_DEVICE.match("/dev/" + name):
                return True
        if len(segments) == 4 \
                and fnmatch.fnmatchcase("dev", segments[0]) \
                and fnmatch.fnmatchcase("disk", segments[1]) \
                and any(fnmatch.fnmatchcase(kind, segments[2])
                        for kind in _DISK_BY_KINDS) \
                and segments[3]:
            return True
        if len(segments) == 3 \
                and fnmatch.fnmatchcase("dev", segments[0]) \
                and fnmatch.fnmatchcase("mapper", segments[1]) \
                and segments[2]:
            return True
    return False


def _raw_device_reason(tokens: list[str]) -> str | None:
    """``cp``/``mv``/``tee``/``install`` writing to a raw disk or memory device.

    ``install`` is the same write spelled as a file install: it copies
    its source onto the destination, so ``install /dev/zero /dev/sda``
    overwrites the disk exactly like ``cp /dev/zero /dev/sda`` — but it
    matched no pattern or token check, since the scan only knew
    ``cp``/``mv``/``tee`` spellings of a raw device write (verified
    against ``_is_dangerous`` before this fix). Destination rules match
    ``cp``: last operand, or ``-t``/``--target-directory`` when given
    (attached and clustered spellings included).
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        if name not in ("cp", "mv", "tee", "install"):
            continue
        args = _args_until_separator(tokens, i + 1)
        operands: list[str] = []
        target_dir: str | None = None
        j = 0
        while j < len(args):
            arg = args[j]
            if arg == "--":
                operands.extend(args[j + 1:])
                break
            if arg in ("-t", "--target-directory"):
                if j + 1 < len(args):
                    target_dir = args[j + 1]
                j += 2
                continue
            if arg.startswith("--target-directory="):
                target_dir = arg.split("=", 1)[1]
                j += 1
                continue
            if name == "install" and arg in _INSTALL_VALUE_OPTIONS:
                j += 2
                continue
            if name == "install" and arg.startswith(
                    tuple(o + "=" for o in _INSTALL_VALUE_OPTIONS)):
                j += 1
                continue
            if arg.startswith("-") and not arg.startswith("--") and arg != "-":
                # Short-option cluster: a "t" takes its value from the
                # rest of the cluster, if any, else the next word.
                k = arg.find("t", 1)
                if k != -1:
                    if k + 1 < len(arg):
                        target_dir = arg[k + 1:]
                    elif j + 1 < len(args):
                        target_dir = args[j + 1]
                        j += 1
                j += 1
                continue
            operands.append(arg)
            j += 1
        if name == "tee":
            candidates = operands
        elif target_dir is not None:
            candidates = [target_dir]
        elif operands:
            candidates = [operands[-1]]
        else:
            candidates = []
        for dest in candidates:
            if dest and _is_raw_device_spelling(dest):
                return "raw disk write"
    return None


# Commands whose entire purpose is overwriting or erasing their target:
# ``shred`` writes over it, ``wipefs`` strips its filesystem signatures,
# ``blkdiscard`` discards its blocks. Aimed at a raw disk or memory
# device, each destroys it exactly like the ``dd``/``cp`` writes above
# — but none matched any pattern or token check (verified against
# ``_is_dangerous`` before this fix). Only raw-device targets count:
# ``shred /tmp/file`` and ``wipefs /dev/null`` stay allowed, as does
# every flag spelling (value words such as ``shred -n 1``'s ``1`` are
# harmless operands that never match the device pattern).
def _device_wipe_reason(tokens: list[str]) -> str | None:
    """``shred``/``wipefs``/``blkdiscard`` aimed at a raw device."""
    for i, tok in enumerate(tokens):
        if _basename(tok) not in ("shred", "wipefs", "blkdiscard"):
            continue
        args = _args_until_separator(tokens, i + 1)
        operands: list[str] = []
        j = 0
        while j < len(args):
            arg = args[j]
            if arg == "--":
                operands.extend(args[j + 1:])
                break
            if arg.startswith("-") and arg != "-":
                j += 1
                continue
            operands.append(arg)
            j += 1
        for target in operands:
            if target and _is_raw_device_spelling(target):
                return "raw disk wipe"
    return None


def _split_tool_args(args: list[str], value_shorts: frozenset | None = None,
                     value_longs: frozenset | None = None):
    """Split a tool's args into operands, flags, and option values.

    Short-option clusters are scanned left to right; a short flag in
    *value_shorts* consumes the rest of its cluster, or the next word,
    as its value. A long flag in *value_longs* consumes the next word
    unless its value is attached with ``=``. ``--`` ends option
    parsing; a bare ``-`` is an operand. Returns ``(operands,
    short_flags, long_flags, values)`` where *values* is a list of
    ``(flag, value)`` pairs, so a caller can tell an option's value
    (e.g. sgdisk's ``-b`` backup file) from a plain operand.
    """
    value_shorts = value_shorts or frozenset()
    value_longs = value_longs or frozenset()
    operands: list[str] = []
    shorts: set[str] = set()
    longs: set[str] = set()
    values: list[tuple[str, str]] = []
    j = 0
    while j < len(args):
        arg = args[j]
        if arg == "--":
            operands.extend(args[j + 1:])
            break
        if arg.startswith("--") and len(arg) > 2:
            name, eq, val = arg[2:].partition("=")
            longs.add(name)
            if eq:
                values.append((name, val))
            elif name in value_longs and j + 1 < len(args):
                values.append((name, args[j + 1]))
                j += 1
            j += 1
            continue
        if arg.startswith("-") and arg != "-":
            consumed_next = False
            k = 1
            while k < len(arg):
                ch = arg[k]
                shorts.add(ch)
                if ch in value_shorts:
                    if k + 1 < len(arg):
                        values.append((ch, arg[k + 1:]))
                    elif j + 1 < len(args):
                        values.append((ch, args[j + 1]))
                        consumed_next = True
                    break
                k += 1
            j += 2 if consumed_next else 1
            continue
        operands.append(arg)
        j += 1
    return operands, shorts, longs, values


# Format / partition / volume tools that destroy the raw device they are
# aimed at: only ``mkfs`` was caught (by a regex), so ``mke2fs``,
# ``mkswap``, ``fdisk``, ``parted``, ``sgdisk``, ``sfdisk``,
# ``cryptsetup luksFormat``, ``pvcreate``/``vgcreate`` and their kin all
# passed the blocklist (verified against ``_is_dangerous`` before this
# fix). Each is blocked only on a raw-device target — ``mke2fs
# /tmp/disk.img`` stays allowed — and each keeps its read-only form:
# ``fdisk -l``, ``sfdisk -d``, ``parted print``, ``sgdisk -p``,
# ``pvdisplay``/``vgdisplay``, ``cryptsetup luksDump`` and the dry runs
# (``mke2fs -n``, ``zpool create -n``). The same hole stayed open for
# the tools that destroy a disk without partitioning or formatting it:
# ``cryptsetup erase`` (wipes the LUKS header), ``nvme format`` /
# ``nvme sanitize`` (erase the whole namespace), ``hdparm
# --security-erase`` (firmware secure erase) and ``badblocks -w``
# (destructive write test) — all four passed unchecked (verified
# against ``_is_dangerous`` before this fix), while their read-only
# forms (``nvme list`` / ``smart-log``, ``hdparm -I``,
# ``badblocks`` without ``-w``, ``cryptsetup luksDump``) stay allowed.
def _format_device_reason(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name == "mke2fs":
            # -n is a dry run (prints what would be done, writes
            # nothing); every other form creates a filesystem on the
            # device. Value options per mke2fs usage (-b, -C, -d, -e,
            # -E, -g, -i, -I, -J, -L, -M, -N, -o, -O, -r, -t, -T, -U,
            # -z).
            operands, shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("bCdeEgiIJLMNOortTUz"))
            if "n" in shorts:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "filesystem format"
        elif name == "mkswap":
            # No dry run exists: -c only checks bad blocks before
            # writing the swap header. Value options per mkswap --help
            # (-p pagesize, -L label, -v swapversion, -U uuid,
            # -e endianness).
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("pLUve"),
                value_longs=frozenset(
                    ("pagesize", "label", "swapversion", "uuid",
                     "endianness")))
            if any(_is_raw_device_spelling(o) for o in operands):
                return "swap format"
        elif name == "fdisk":
            # -l lists, -v prints the version, -x lists with extra
            # detail: all read-only. Anything else opens the disk for
            # editing (the partition table is written on `w`). Value
            # options per fdisk --help (-b sector size, -C cylinders,
            # -H heads, -S sectors, -o output, -u units, -w wipe);
            # -L/--color takes an optional argument, so it is NOT
            # value-taking here — `-L /dev/sda` leaves the device an
            # operand, and consuming it would be a false pass.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("bCHSouw"),
                value_longs=frozenset(
                    ("sector-size", "cylinders", "heads", "sectors",
                     "output", "units", "wipe")))
            if shorts & {"l", "v", "x"} or longs & {
                    "list", "version", "list-details"}:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "partition table edit"
        elif name == "parted":
            # parted [options] [device [command]]: -l lists all devices
            # (read-only). With a raw device, the subcommand decides:
            # print / help / version / align-check / unit / select
            # write nothing; no subcommand enters an interactive
            # session on the disk, and anything else (mklabel, mkpart,
            # rm, resizepart, set, ...) rewrites the partition table.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("a"),
                value_longs=frozenset(("align",)))
            if "l" in shorts or "list" in longs:
                continue
            if not operands:
                continue
            if not _is_raw_device_spelling(operands[0]):
                continue
            if len(operands) < 2:
                return "partition table edit"
            if operands[1] in ("print", "help", "version", "align-check",
                               "unit", "select"):
                continue
            return "partition table edit"
        elif name == "sgdisk":
            # -p prints, -v verifies, -i shows info, -L lists types:
            # read-only. Every other operation (-Z zap-all, -z zap, -o
            # clear, -g/-m convert, -e move header, -d delete, -n new,
            # -c rename, -t typecode, -u guid, -s sort, -A attributes,
            # -l load-backup, -R replicate, -x expert) rewrites the
            # table, and a bare `sgdisk /dev/sda` names the disk for
            # modification. The -b backup file is a write too, so a
            # raw-device backup target blocks even with a read-only op.
            operands, shorts, longs, values = _split_tool_args(
                args,
                value_shorts=frozenset("bcdintuAa"),
                value_longs=frozenset(
                    ("backup", "change-name", "delete", "info", "new",
                     "typecode", "partition-guid", "attributes",
                     "alignment", "load-backup", "replicate")))
            if any(_is_raw_device_spelling(v)
                   for flag, v in values if flag in ("b", "backup")):
                return "raw disk write"
            if not any(_is_raw_device_spelling(o) for o in operands):
                continue
            if shorts & {"Z", "z", "o", "g", "m", "e", "d", "n", "c",
                         "t", "u", "s", "A", "l", "R", "x"} \
                    or longs & {"zap-all", "zap", "clear",
                                "convert-to-gpt", "convert-to-mbr",
                                "move-second-header", "delete-partitions",
                                "new", "change-name", "typecode",
                                "partition-guid", "sort", "attributes",
                                "load-backup", "replicate", "expert"}:
                return "partition table edit"
            if shorts & {"p", "v", "i", "L"} or longs & {
                    "print", "verify", "info", "list-types"}:
                continue
            return "partition table edit"
        elif name == "sfdisk":
            # -d dumps, -l lists, -V verifies, -s prints the size, -g
            # shows geometry, -J dumps JSON, -T lists types, --disk-id
            # prints the id: read-only. --delete removes partitions,
            # and a bare `sfdisk /dev/sda` reads a partition script
            # from stdin and applies it. The -b backup file is a write
            # too, so a raw-device backup target blocks even with a
            # read-only op.
            operands, shorts, longs, values = _split_tool_args(
                args,
                value_shorts=frozenset("NoXuYbCHSw"),
                value_longs=frozenset(
                    ("output", "units", "label", "label-nested",
                     "backup", "cylinders", "heads", "sectors",
                     "wipe")))
            if any(_is_raw_device_spelling(v)
                   for flag, v in values if flag in ("b", "backup")):
                return "raw disk write"
            if not any(_is_raw_device_spelling(o) for o in operands):
                continue
            if "delete" in longs:
                return "partition table edit"
            if shorts & {"d", "l", "V", "s", "g", "J", "T"} or longs & {
                    "dump", "list", "verify", "print-size",
                    "show-geometry", "json", "list-types", "disk-id"}:
                continue
            return "partition table edit"
        elif name == "cryptsetup":
            # Only the data-destroying subcommands: luksFormat wipes
            # the device, luksHeaderRestore overwrites the header,
            # luksRemoveKey / luksKillSlot destroy access to the data,
            # and erase wipes the LUKS header outright. Everything else
            # (luksDump, isLuks, status, open, reencrypt, ...) is left
            # alone: reencrypt rewrites the device in place but
            # preserves the data, so it is a maintenance operation,
            # not a destructive one. The device is the operand after
            # the subcommand; value options (-d/--key-file, --header,
            # --type, --cipher, ...) are skipped so their values are
            # not mistaken for it.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("d"),
                value_longs=frozenset(
                    ("key-file", "header", "master-key-file",
                     "volume-key-file", "type", "cipher", "hash",
                     "key-size", "sector-size", "uuid", "key-slot",
                     "token-id", "priority", "pbkdf", "iter-time",
                     "use-random", "use-urandom", "keyfile-size",
                     "offset", "skip", "size", "timeout", "tries")))
            if not operands:
                continue
            subcommand = operands[0]
            if subcommand == "luksFormat":
                reason = "disk encryption format"
            elif subcommand == "luksHeaderRestore":
                reason = "LUKS header restore"
            elif subcommand in ("luksRemoveKey", "luksKillSlot"):
                reason = "LUKS key removal"
            elif subcommand == "erase":
                reason = "LUKS header erase"
            else:
                continue
            if any(_is_raw_device_spelling(o) for o in operands[1:]):
                return reason
        elif name in ("pvcreate", "pvremove"):
            # pvcreate writes LVM metadata onto the device; pvremove
            # strips it. Value options: -u/--uuid, --metadatatype,
            # --pvmetadatacopies, --dataalignment, --restorefile,
            # --labelsector, --setphysicalvolumesize.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("u"),
                value_longs=frozenset(
                    ("uuid", "metadatatype", "pvmetadatacopies",
                     "dataalignment", "restorefile", "labelsector",
                     "setphysicalvolumesize", "vgmetadatacopies")))
            if any(_is_raw_device_spelling(o) for o in operands):
                return "LVM metadata write"
        elif name in ("vgcreate", "vgremove", "vgextend", "vgreduce"):
            # vgcreate claims the devices for a new volume group;
            # vgextend adds one to an existing group; vgremove /
            # vgreduce drop one. Value options: -s physical extent
            # size, -l max logical volumes, -p max physical volumes,
            # --metadatatype, --vgmetadatacopies.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("slp"),
                value_longs=frozenset(
                    ("physicalextentsize", "maxlogicalvolumes",
                     "maxphysicalvolumes", "metadatatype",
                     "vgmetadatacopies")))
            if any(_is_raw_device_spelling(o) for o in operands):
                return "LVM metadata write"
        elif name == "lvremove":
            # Removes a logical volume: its path is /dev/mapper/vg-lv
            # (a raw-device spelling) or /dev/<vg>/<lv>. The two-level
            # shape is shared with /dev/pts/* and /dev/shm/*, but those
            # are never valid lvremove targets, so the over-block only
            # ever hits a nonsense invocation.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            for target in operands:
                if _is_raw_device_spelling(target):
                    return "LVM metadata write"
                segments = [s for s in _normalize_path_spelling(target)
                            .split("/") if s != ""]
                if len(segments) == 3 and segments[0] == "dev" \
                        and segments[1] not in ("disk", "mapper") \
                        and segments[2]:
                    return "LVM metadata write"
        elif name == "mdadm":
            # Only --create builds a new array (destroying the member
            # devices); --detail / --examine / --assemble / --stop only
            # read or manage an existing one. Value options: -l level,
            # -n raid-devices, -c chunk, -e metadata, -N name, -b
            # bitmap, -p layout.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("lnceNbp"),
                value_longs=frozenset(
                    ("level", "raid-devices", "chunk", "name",
                     "metadata", "bitmap", "layout", "spare-devices",
                     "write-journal", "journal", "homehost", "nodes")))
            if "C" not in shorts and "create" not in longs:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "RAID array create"
        elif name == "zpool":
            # Only `create` builds a pool on the devices; status /
            # list / get / iostat only read. -n is a dry run (prints
            # what would be done, writes nothing).
            operands, shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("oOmRt"),
                value_longs=frozenset())
            if not operands or operands[0] != "create":
                continue
            if "n" in shorts:
                continue
            if any(_is_raw_device_spelling(o) for o in operands[1:]):
                return "ZFS pool create"
        elif name == "nvme":
            # Only `format` and `sanitize` destroy the namespace:
            # format erases every block, sanitize crypto-erases /
            # block-erases the whole device. Everything else (list,
            # smart-log, id-ctrl, id-ns, get-feature, ...) only reads.
            # Value options per nvme --help: format takes -s/--ses,
            # -l/--lbaf, -i/--pi, -p/--pil, -m/--ms, -n/--namespace-id;
            # sanitize takes -a/--sanact and -p/--ovrpat. A value is
            # never a device in legitimate use, so a device spelling
            # in either position blocks — it is either an evasion
            # attempt (``-a /dev/nvme0n1``) or nonsense that would
            # fail in nvme anyway.
            operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("slipmna"),
                value_longs=frozenset(
                    ("ses", "lbaf", "pi", "pil", "ms",
                     "namespace-id", "sanact", "ovrpat")))
            if not operands or operands[0] not in ("format",
                                                    "sanitize"):
                continue
            targets = list(operands[1:]) + [v for _f, v in values]
            if any(_is_raw_device_spelling(o) for o in targets):
                return ("NVMe namespace format" if operands[0] == "format"
                        else "NVMe sanitize")
        elif name == "hdparm":
            # Only the firmware secure-erase forms destroy the disk:
            # --security-erase / --security-erase-enhanced wipe every
            # sector (the password is the option's value, consumed
            # above so it is never mistaken for the device). The
            # read-only forms (-I, -g, --security-freeze is a lock,
            # not a wipe) and password management (--security-set-pass,
            # --security-unlock, --security-disable) stay allowed:
            # they lock or unlock the drive but destroy no data.
            operands, _shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset(""),
                value_longs=frozenset(
                    ("security-erase", "security-erase-enhanced",
                     "security-set-pass", "security-unlock",
                     "security-disable", "user-master")))
            if not longs & {"security-erase",
                           "security-erase-enhanced"}:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "firmware secure erase"
        elif name == "badblocks":
            # Only -w is a destructive write test: it writes test
            # patterns over every block. The default read-only scan
            # and -n (non-destructive read-write: patterns are written
            # then the original data restored) stay allowed. Value
            # options per badblocks usage (-b block size, -c blocks at
            # once, -e max bad blocks, -o output file, -t test
            # pattern, -i input file).
            operands, shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("bceoti"),
                value_longs=frozenset())
            if "w" not in shorts:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "destructive disk test"
    return None


# A quote the shell strips before acting, allowed between the pieces of
# a device path in the redirect / ``dd of=`` patterns below: ``"/dev/sda"``,
# ``'/dev/sda'``, ``/dev/"sda"``, ``/"dev/sda"`` and ``$'/dev/sda'``
# (re-wrapped in quotes by ``_expand_ansi_c_quoting``) all name the device
# exactly like ``/dev/sda`` — but the literal ``/dev/`` prefix in those
# patterns never matched them (verified against ``_is_dangerous`` before
# this fix). Only the device-path joints admit a quote, so a quoted
# operator stays literal: ``echo '>' /dev/sda`` is still allowed, and
# so is a quoted harmless path such as ``"/tmp/out.txt"``.
_Q = r"""["']?"""


# One path segment inside a ``..`` climb, and the ``..`` segment itself,
# each with a quote admitted at its joints the way the device names
# admit them above.
_CLIMB_SEG = _Q + r"""[^/\s"']+""" + _Q
_CLIMB_DOTDOT = _Q + r"\.\." + _Q


# A ``..`` climb that lands back on a device: the shell resolves the
# segments lexically (the way ``_normalize_path_spelling`` does for the
# token checks), so ``/dev/../dev/sda`` and ``/dev/foo/../../dev/sda``
# name ``/dev/sda`` — but the redirect patterns above only admit dot /
# empty segments after ``dev/``, so every such spelling slipped past
# them (verified against ``_is_dangerous`` before this fix), even
# though the ``dd of=`` pattern (which only needs the ``/dev/``
# prefix) and the copy / wipe token checks (which normpath the target)
# already blocked their own ``..`` spellings. Two forms land back in
# ``/dev``: (a) climb, then re-enter a ``dev/`` directory — any run of
# segments, one ``..``, any run of segments, then a literal ``dev/``;
# (b) one or more ``<dir>/../`` pairs — ``/dev/mapper/../sda``,
# ``/dev/a/b/../../sda``. The climb is treated lexically, the same way
# the token checks are: ``/dev/fd/../sda`` is blocked too even though
# ``fd`` is a symlink (the redirect would fail there anyway), and a
# second ``dev/`` swallowed as a climb segment (``/dev/../dev/dev/sda``)
# is blocked the same way. The tail is the same three families the
# patterns above know (disk names, ``mem``/``kmem``/``port``, by-*/
# mapper paths), so the brace re-check in ``_is_dangerous`` covers
# brace spellings of the climb too.
_CLIMB = (
    r"(?:"
    r"(?:(?:" + _CLIMB_SEG + r"/)*" + _CLIMB_DOTDOT + r"/(?:"
    + _CLIMB_SEG + r"/)*" + _Q + r"dev/)"
    r"|(?:(?:" + _CLIMB_SEG + r"/" + _CLIMB_DOTDOT + r"/)+)"
    r")"
)


# Redirects to a raw device (``>`` / ``>|`` / ``>>``): the device-name
# tail admits a quote between letters (``/dev/s"d"a``), and the
# ``mem``/``kmem``/``port`` name keeps its word-boundary behaviour via
# the ``(?![a-z0-9])`` lookahead after an optional trailing quote, so
# ``/dev/memory`` (quoted or not) stays allowed. The prefix group also
# covers the device-mapper / loop / RAID / ramdisk / network-block
# families (``dm-0``, ``loop0``, ``md0``, ``ram0``, ``nbd0``,
# ``rbd0``) and ``mmcblk`` — the ``mmcblk`` redirect slipped past
# until now even though the copy / wipe checks already knew it
# (verified against ``_is_dangerous`` before this fix); the tail
# admits the hyphen in ``dm-0``. Kept at module level so the
# brace-spelling re-check in ``_is_dangerous`` can reuse the same
# patterns instead of duplicating them; the spelling rules are
# explained where they are used, above the ``dd`` pattern.
_DEVICE_REDIRECT_PATTERNS = [
    (r">\|?\s*" + _Q + r"/+" + _Q + r"dev/" + _Q + r"(?:\./|/)*" + _Q
     + r"(s" + _Q + r"d|h" + _Q + r"d|n" + _Q + r"v" + _Q + r"m" + _Q
     + r"e|v" + _Q + r"d|d" + _Q + r"m|l" + _Q + r"o|m" + _Q + r"m"
     + _Q + r"c" + _Q + r"b" + _Q + r"l" + _Q + r"k|m" + _Q + r"d"
     + r"|r" + _Q + r"a|r" + _Q + r"b|n" + _Q + r"b)(?:" + _Q
     + r"[a-z0-9-])*",
     "raw disk write"),
    # The ``\b`` used to sit before the whole optional group, so it
    # also required a word boundary before a plain ``>`` — and a spaced
    # redirect (``cat x > /dev/mem``, space before ``>``) never matched
    # at all. The boundary now applies only to the ``sha256sum``
    # alternative, where it belongs.
    (r"(?:\bsha256sum\s+)?>\|?\s*" + _Q + r"/+" + _Q + r"dev/" + _Q
     + r"(?:\./|/)*" + _Q
     + r"(m" + _Q + r"e" + _Q + r"m|k" + _Q + r"m" + _Q + r"e" + _Q
     + r"m|p" + _Q + r"o" + _Q + r"r" + _Q + r"t)" + _Q
     + r"(?![a-z0-9])",
     "raw device write"),
    # Stable device symlinks and device-mapper nodes as redirect
    # targets: ``> /dev/disk/by-id/ata-…`` and ``> /dev/mapper/vg-lv``
    # write the disk exactly like ``> /dev/sda`` — but the literal
    # device-name position in the patterns above never matched a
    # by-*/mapper path, and the names are arbitrary, so no fixed name
    # can be listed (verified against ``_is_dangerous`` before this
    # fix). The directory words admit one quote at each joint, the way
    # the device names above do; the name itself is one word of
    # device-name characters, each optionally quote-prefixed. A bare
    # ``/dev/disk/by-id`` or ``/dev/mapper`` directory (no name after
    # it) is not a device and does not match.
    (r">\|?\s*" + _Q + r"/+" + _Q + r"dev/" + _Q + r"(?:\./|/)*" + _Q
     + r"(?:d" + _Q + r"i" + _Q + r"s" + _Q + r"k/" + _Q + r"b" + _Q
     + r"y" + _Q + r"-" + _Q + r"(?:i" + _Q + r"d"
     + r"|p" + _Q + r"a" + _Q + r"t" + _Q + r"h"
     + r"|p" + _Q + r"a" + _Q + r"r" + _Q + r"t" + _Q + r"u" + _Q
     + r"u" + _Q + r"i" + _Q + r"d"
     + r"|l" + _Q + r"a" + _Q + r"b" + _Q + r"e" + _Q + r"l"
     + r"|u" + _Q + r"u" + _Q + r"i" + _Q + r"d"
     + r"|p" + _Q + r"a" + _Q + r"r" + _Q + r"t" + _Q + r"l" + _Q
     + r"a" + _Q + r"b" + _Q + r"e" + _Q + r"l"
     + r")" + _Q + r"/"
     + r"|m" + _Q + r"a" + _Q + r"p" + _Q + r"p" + _Q + r"e" + _Q + r"r"
     + _Q + r"/)"
     + r"(?:" + _Q + r"[A-Za-z0-9_.:+-])+",
     "raw disk write"),
    # ``..`` climbs back onto a device (see ``_CLIMB`` above):
    # ``> /dev/../dev/sda``, ``> /dev/mapper/../sda``,
    # ``> /dev/foo/../../dev/sda``. Precision is preserved: a climb
    # that lands outside ``/dev`` (``> /dev/../sda`` is a plain file
    # in ``/``, ``> /dev/disk/../../sda`` likewise) stays allowed, as
    # does a climb that lands on a non-device
    # (``> /dev/a/b/../sda`` is ``/dev/a/sda``,
    # ``> /dev/../dev/null`` is ``/dev/null``).
    (r">\|?\s*" + _Q + r"/+" + _Q + r"dev/" + _Q + _CLIMB + _Q
     + r"(?:"
     + r"(s" + _Q + r"d|h" + _Q + r"d|n" + _Q + r"v" + _Q + r"m" + _Q
     + r"e|v" + _Q + r"d|d" + _Q + r"m|l" + _Q + r"o|m" + _Q + r"m"
     + _Q + r"c" + _Q + r"b" + _Q + r"l" + _Q + r"k|m" + _Q + r"d"
     + r"|r" + _Q + r"a|r" + _Q + r"b|n" + _Q + r"b)(?:" + _Q
     + r"[a-z0-9-])*"
     + r"|(m" + _Q + r"e" + _Q + r"m|k" + _Q + r"m" + _Q + r"e" + _Q
     + r"m|p" + _Q + r"o" + _Q + r"r" + _Q + r"t)" + _Q
     + r"(?![a-z0-9])"
     + r"|(?:d" + _Q + r"i" + _Q + r"s" + _Q + r"k/" + _Q + r"b" + _Q
     + r"y" + _Q + r"-" + _Q + r"(?:i" + _Q + r"d"
     + r"|p" + _Q + r"a" + _Q + r"t" + _Q + r"h"
     + r"|p" + _Q + r"a" + _Q + r"r" + _Q + r"t" + _Q + r"u" + _Q
     + r"u" + _Q + r"i" + _Q + r"d"
     + r"|l" + _Q + r"a" + _Q + r"b" + _Q + r"e" + _Q + r"l"
     + r"|u" + _Q + r"u" + _Q + r"i" + _Q + r"d"
     + r"|p" + _Q + r"a" + _Q + r"r" + _Q + r"t" + _Q + r"l" + _Q
     + r"a" + _Q + r"b" + _Q + r"e" + _Q + r"l"
     + r")" + _Q + r"/"
     + r"|m" + _Q + r"a" + _Q + r"p" + _Q + r"p" + _Q + r"e" + _Q + r"r"
     + _Q + r"/)"
     + r"(?:" + _Q + r"[A-Za-z0-9_.:+-])+"
     + r")",
     "raw disk write"),
]


def _is_dangerous(command: str, _depth: int = 0) -> str | None:
    """Blocklist scan. Returns a reason string, or None if the command passes.

    This is a blocklist, not a sandbox: it stops the obvious footguns, not a
    determined adversary. The permission gate is the real protection, and
    real isolation belongs in a container layer. The scan is spelling-aware
    (``/bin/rm`` counts as ``rm``), covers ``$HOME``/``~`` targets and
    ``$'...'`` ANSI-C-quoted spellings of them, splits shell operators
    glued to neighbouring words (``/;``, ``(rm``, ``hi;rm``, backtick
    substitution), recurses into nested ``sh -c``/``eval`` payloads —
    including attached (``sh -c'...'``), clustered (``sh -ec '...'``)
    and option-preceded (``bash --norc -c '...'``) ``-c`` spellings —
    and catches ``find / -delete``, download-piped-to-shell one-liners,
    ``cp``/``mv``/``tee``/``install`` writes to a raw disk or memory
    device, ``shred``/``wipefs``/``blkdiscard`` aimed at one, and
    format / partition / volume tools aimed at one (``mke2fs``,
    ``mkswap``, ``fdisk``, ``parted``, ``sgdisk``, ``sfdisk``,
    ``cryptsetup luksFormat`` / ``erase``, ``pvcreate``/``vgcreate``
    and kin, ``nvme format`` / ``sanitize``, ``hdparm
    --security-erase``, ``badblocks -w`` — each keeping its read-only
    form) — including dot-segment /
    doubled-slash spellings of the device
    (``/dev/./sda``, ``//dev/sda``), ``>|`` redirects to one, quoted
    spellings of the device path (``"/dev/sda"``, ``/dev/"sda"``), and
    glob / brace spellings (``/dev/sd[a]``, ``/dev/{sda,sdb}``) — and
    device-mapper / loop / RAID / ramdisk / network-block spellings
    (``/dev/dm-0``, ``/dev/loop0``, ``/dev/md0``, ``/dev/ram0``,
    ``/dev/nbd0``, ``/dev/rbd0``), stable-symlink spellings
    (``/dev/disk/by-id/ata-…``), and ``/dev/mapper/…`` nodes, in the
    copy / wipe checks and in the ``>`` redirect patterns (which never
    knew ``/dev/mmcblk0`` either) — and ``..`` climbs back onto a
    device in the redirect patterns (``> /dev/../dev/sda``,
    ``> /dev/mapper/../sda``, ``> /dev/foo/../../dev/sda``), which
    the ``dd of=`` pattern and the copy / wipe checks already blocked.
    """
    command = _expand_ansi_c_quoting(command)
    patterns = [
        (r":\(\)\s*\{", "fork bomb"),
        (r"\bmkfs(\.\w+)?\b", "filesystem format"),
        # The raw-device redirect/``dd`` patterns are spelling-aware
        # the same way the token checks now are: the shell resolves
        # ``//``, ``/./`` and ``//`` inside the path before acting, so
        # ``of=//dev/sda``, ``> //dev/sda``, ``> /dev/./sda`` and
        # ``> /dev//sda`` name the device exactly like the canonical
        # spelling but missed the literal ``/dev/`` prefix — and ``>|``
        # (clobber override) missed the ``>``-then-space shape entirely.
        # ``(?:\./|/)*`` after ``dev/`` admits only dot/empty segments,
        # so ``> /dev/../sda`` (a plain file in ``/``) stays allowed.
        # ``_Q`` admits one quote at each joint of the device path (see
        # its comment above): the shell strips quotes before acting, so
        # ``> "/dev/sda"``, ``> /dev/"sda"`` and ``of="/dev/sda"`` name
        # the device too, but used to miss the literal prefix. The
        # device-name tail admits a quote between letters as well
        # (``/dev/s"d"a``); the ``mem``/``kmem``/``port`` name keeps its
        # word-boundary behaviour via the ``(?![a-z0-9])`` lookahead
        # after an optional trailing quote, so ``/dev/memory`` (quoted
        # or not) stays allowed.
        (r"\bdd\b[^\n|;&]*\bof=" + _Q + r"/+" + _Q
         + r"dev/(?:\./|/)*", "raw disk write"),
        # The redirect patterns live at module level as
        # ``_DEVICE_REDIRECT_PATTERNS`` (reused by the brace-spelling
        # re-check below); the spelling rules above apply to them too.
        *_DEVICE_REDIRECT_PATTERNS,
        (r"\b(shutdown|reboot|halt|poweroff)\b", "power control"),
        # The permission-nuke pattern used to require trailing whitespace
        # after "/": the plain "chmod -R 777 /" (end of command, or "/;"
        # before the next command) slipped past it. The terminator is now
        # whitespace, end of command, or a shell separator, and any flag
        # spellings (-R, -Rf, --recursive) are accepted before the mode,
        # so only the root dir itself is guarded — /tmp or /home stay
        # allowed, as before. The numeric mode is any octal run ending
        # in 777, not just 777/0777: leading zeros (00777) and the
        # special-bit digit (4777, 7777, 1777, 07777) spell the same
        # mode and used to slip past the single-optional-zero pattern.
        # Symbolic modes that leave / world-writable are caught by
        # _chmod_reason below, on the tokenized command.
        (r"\bchmod\s+(?:-\S+\s+)*[0-7]*777\s+/+(?:\s|$|[;&|])",
         "permission nuke"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, command):
            return reason
    # Brace spellings of a redirect target: the shell expands braces
    # before acting, so ``> /dev/{sda,sdb}`` and ``>> /dev/{sda,sdb}``
    # redirect to /dev/sda — but the literal device-name position in
    # the redirect patterns never matched the leading ``{`` (verified
    # against ``_is_dangerous`` before this fix). A redirect target is
    # a single word, so only its FIRST expansion can be the target:
    # re-check the same patterns against the first-expansion spelling
    # of the command. A brace group with no comma (``{sda}``) is
    # literal in the shell and ``_expand_braces`` leaves it alone, so
    # it stays allowed — as does a harmless first alternative
    # (``{null,sda}`` redirects to /dev/null).
    first_expansion = _expand_braces(command)[0]
    if first_expansion != command:
        for pattern, reason in _DEVICE_REDIRECT_PATTERNS:
            if re.search(pattern, first_expansion):
                return reason
    if _PIPE_TO_SHELL.search(command):
        return "download piped to a shell"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None  # unparseable; the shell will report the syntax error
    tokens = _split_shell_operators(tokens)
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
            or _chmod_reason(tokens) or _raw_device_reason(tokens)
            or _device_wipe_reason(tokens)
            or _format_device_reason(tokens)
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
