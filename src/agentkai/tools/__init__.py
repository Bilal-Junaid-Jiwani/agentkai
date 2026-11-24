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
import http.client
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
    t = _expand_user_vars(tok)
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


# The system trees a recursive delete must never take, named
# explicitly. The guarded set used to be derived from passwd alone
# (``/``, the homes, their parents), which incidentally covered
# ``/var`` / ``/usr`` on machines whose system accounts live there
# but left ``/etc``, ``/opt`` and ``/srv`` unguarded everywhere:
# ``rm -rf /etc`` passed the scan and destroyed the machine's
# configuration (verified against ``_is_dangerous`` before this
# fix). Naming the trees makes the protection identical on every
# machine. Subdirectories stay the caller's responsibility, exactly
# like ``$HOME/project``: ``rm -rf /etc/myapp/cache`` and
# ``rm -rf /var/tmp/scratch`` still pass.
_SYSTEM_DIRS = frozenset({
    "/etc", "/usr", "/var", "/opt", "/srv", "/boot", "/bin", "/sbin",
    "/lib", "/lib64", "/sys", "/proc", "/dev", "/root",
})


def _guarded_dirs() -> set[str]:
    """``/``, every account home, the directory holding the homes,
    and the system trees in ``_SYSTEM_DIRS``."""
    protected = {"/"} | _home_dirs()
    parents = {
        posixpath.dirname(home)
        for home in protected
        if home != "/" and posixpath.dirname(home) not in ("", "/")
    }
    return protected | parents | set(_SYSTEM_DIRS)


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
    spellings = [t]
    if _USER_VAR.search(t):
        # A $USER/$LOGNAME component names the current account, and
        # the directory it picks out under a homes parent is that
        # account's home — spelled with the name the home directory
        # actually has, which need not equal the account name (a
        # root-run agent with HOME=/home/<name> is exactly that
        # case). Price that spelling too.
        home_name = posixpath.basename(str(Path.home()))
        if home_name:
            spellings.append(_USER_VAR.sub(home_name, t))
    for spelling in spellings:
        if _spelling_is_guarded(spelling, guarded):
            return True
    return False


def _spelling_is_guarded(t: str, guarded: set[str]) -> bool:
    """One spelling of a target against the guarded set (see
    ``_is_root_or_home`` for the spelling rules)."""
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


def _realpath_of(tok: str) -> str | None:
    """The resolved path of an absolute literal target, if it moves.

    The destructive-verb checks compare path *spellings*, so a
    symlink spelling hid the target entirely: ``ln -s /dev/sda
    /tmp/disklink`` is unchecked (a link is just a name), and ``dd
    of=/tmp/disklink`` then wrote the raw device through it, while
    ``rm -rf /tmp/homelink/`` (a link to a home, trailing slash)
    deleted the home's contents (verified against ``_is_dangerous``
    before this fix). Resolving the target with ``os.path.realpath``
    — metadata only, nothing is opened for writing — prices the
    spelling the shell will act on. Relative spellings, expansions
    and globs return None: their resolution depends on a working
    directory the scan does not know, and the other checks price
    those spellings themselves.
    """
    spell = tok.strip("\"'")
    if not spell.startswith("/") or "$" in spell or _has_glob(spell):
        return None
    resolved = os.path.realpath(spell)
    return resolved if resolved != spell else None


def _realpath_is_raw_device(tok: str) -> bool:
    """True if *tok* is a symlink (chain) landing on a raw device."""
    resolved = _realpath_of(tok)
    return resolved is not None and _is_raw_device_spelling(resolved)


def _realpath_is_guarded(tok: str) -> bool:
    """True if *tok* is a symlink (chain) landing on a guarded dir."""
    resolved = _realpath_of(tok)
    return resolved is not None and _is_root_or_home(resolved)


def _args_until_separator(tokens: list[str], start: int) -> list[str]:
    """Tokens from *start* up to (not including) the next shell separator."""
    out: list[str] = []
    for tok in tokens[start:]:
        if tok in _SEPARATORS:
            break
        out.append(tok)
    return out


_BARE_VARIABLE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def _rm_reason(tokens: list[str]) -> str | None:
    """Recursive rm aimed at / or the home dir — however rm is spelled.

    Matches ``rm`` by basename so ``/bin/rm`` and ``sudo /bin/rm`` are
    caught too; precise about targets so `rm -rf ./build` passes.

    Three spellings the old scan missed (verified against
    ``_is_dangerous`` before this fix):

    - ``-R``: GNU rm treats it exactly like ``-r`` (POSIX specifies
      both), but the recursive test only looked for lowercase ``r``,
      so ``rm -Rf /`` sailed through.
    - A target that is one bare unresolved variable (``rm -rf $T``):
      same-command assignments are priced by
      ``_resolve_simple_assignments`` before this runs, so a variable
      still standing here gets its value from the environment the
      scan cannot see — with recursion and force both requested,
      there is no spelling left to price, and the delete is refused.
    - A target spelled through a symlink into a guarded dir
      (``rm -rf /tmp/homelink/``): priced by ``_realpath_is_guarded``.
    """
    for i, tok in enumerate(tokens):
        if _basename(tok) != "rm":
            continue
        args = _args_until_separator(tokens, i + 1)
        recursive = any(
            t == "--recursive"
            or (t.startswith("-") and not t.startswith("--")
                and ("r" in t[1:] or "R" in t[1:]))
            for t in args)
        force = any(
            t == "--force"
            or (t.startswith("-") and not t.startswith("--")
                and "f" in t[1:])
            for t in args)
        targets = [t for t in args if not t.startswith("-")]
        if recursive and any(
                _is_root_or_home(t) or _realpath_is_guarded(t)
                for t in targets):
            return "recursive delete of / or home directory"
        if recursive and force and any(
                _BARE_VARIABLE.match(t.strip("\"'")) for t in targets):
            return "recursive delete of an unresolved variable target"
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
        if any(_is_root_or_home(p) or _realpath_is_guarded(p)
               for p in paths):
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


def _normalize_command(command: str) -> str:
    """Collapse the shell spellings the raw patterns cannot see across.

    Two spellings are rewritten before every check, the way the shell
    itself rewrites them before acting (verified against
    ``_is_dangerous`` before this fix):

    - Line continuations: a backslash-newline joins two physical
      lines into one logical command, so ``dd if=/dev/zero \\<newline>
      of=/dev/sda`` writes the device — but the ``dd`` pattern's
      ``[^\\n…]`` span cannot cross the newline, and ``dd`` had no
      token-stage check to fall back on. Joining the lines first
      hands every later check the command the shell actually runs.
    - ``$IFS`` / ``${IFS}``: the shell word-splits on the expansion,
      so ``rm${IFS}-rf${IFS}/`` is ``rm -rf /`` — but the literal
      token matches no command or flag check. Replacing the expansion
      with a plain space (its first character, and the only one that
      matters for word splitting here) spells the command the shell
      sees. ``$IFS`` followed by a name character is a different
      variable (``$IFSx``) and is left alone.
    """
    command = command.replace("\\\r\n", "").replace("\\\n", "")
    command = command.replace("${IFS}", " ")
    return re.sub(r"\$IFS(?![A-Za-z0-9_])", " ", command)


def _current_username() -> str:
    """The account name the exec shell will expand ``$USER`` to."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001 - fall back to the environment
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


_USER_VAR = re.compile(r"\$\{USER\}|\$USER(?![A-Za-z0-9_])"
                       r"|\$\{LOGNAME\}|\$LOGNAME(?![A-Za-z0-9_])")


def _expand_user_vars(tok: str) -> str:
    """Expand ``$USER`` / ``$LOGNAME`` spellings inside a path token.

    The shell expands them to the current account name before acting,
    so ``rm -rf /home/$USER`` wipes the caller's home — but the path
    checks compared the literal ``$USER`` spelling and let it through
    (verified against ``_is_dangerous`` before this fix). Only these
    two variables are expanded: their value is fixed by the login
    session the exec shell inherits, unlike arbitrary variables,
    which the token-stage assignment pass handles instead.
    """
    user = _current_username()
    if not user:
        return tok
    return _USER_VAR.sub(user, tok)


_BRACED_EXPANSION = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-=+?])([^{}]*)\}")
_PLAIN_EXPANSION = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_vars(text: str, env: dict[str, str]) -> str:
    """Expand the parameter expansions the scan can price exactly.

    ``env`` holds the same-command assignments collected by
    ``_resolve_simple_assignments``. ``${VAR:-word}`` and its kin are
    expanded for *every* variable: with the variable unset (the
    checker's model for a name it has never seen assigned) the shell
    yields ``word`` for the ``-``/``=``/``?`` forms and nothing for
    the ``+`` forms, so ``rm -rf ${D:-/}`` is a delete of ``/`` and
    ``${x:-rm} -rf /`` runs ``rm`` — both slipped past the literal
    checks (verified against ``_is_dangerous`` before this fix).
    ``$HOME``/``${HOME…}`` forms are left untouched: the path
    normalizers price those themselves, with more spellings than a
    plain substitution knows. Plain ``$VAR`` / ``${VAR}`` expand only
    from ``env`` — an unassigned variable's environment value is not
    knowable here, and guessing it would misprice benign commands.
    """
    def braced(m: re.Match) -> str:
        name, op, word = m.group(1), m.group(2), m.group(3)
        if name == "HOME":
            return m.group(0)
        if name in env:
            value = env[name]
            if op in (":+", "+"):
                return word if value else ""
            if op in (":-", ":=", ":?"):
                return value if value else word
            return value  # "-", "=", "?": the value even when empty
        if op in (":+", "+"):
            return ""
        return word

    text = _BRACED_EXPANSION.sub(braced, text)
    return _PLAIN_EXPANSION.sub(
        lambda m: env.get(m.group(1), m.group(0)), text)


def _resolve_simple_assignments(tokens: list[str]) -> list[str]:
    """Price same-command ``NAME=value`` assignments into later tokens.

    The shell evaluates ``T=/; rm -rf $T`` as a delete of ``/`` and
    ``X=rm; $X -rf /`` as an ``rm`` invocation, but the scan compared
    literal tokens and saw neither the verb nor the target (verified
    against ``_is_dangerous`` before this fix). Assignments are only
    collected at a command position (the start of the token stream or
    right after a separator, including the prefix chain before a
    command word), and every later token is rewritten with
    ``_expand_vars``. The assignment tokens themselves are kept
    verbatim — they name no command — and unknown variables are left
    spelled as-is for the other checks to price.
    """
    env: dict[str, str] = {}
    out: list[str] = []
    at_command_start = True
    for tok in tokens:
        if tok in _SEPARATORS:
            out.append(tok)
            at_command_start = True
            continue
        if at_command_start:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", tok,
                         re.DOTALL)
            if m:
                env[m.group(1)] = _expand_vars(m.group(2), env)
                out.append(tok)
                continue
        out.append(_expand_vars(tok, env))
        at_command_start = False
    return out


_SYMBOLIC_CLAUSE = re.compile(r"^([ugoa]*)([+-=])([rwxXstugo]*)$")


def _chmod_symbolic_dir_perms(mode: str) -> dict[str, set[str]] | None:
    """End-state permissions of a directory after symbolic *mode*.

    Clauses apply in order from the directory's usual starting mode
    (755: owner rwx, group/other r-x) — ``=`` replaces a class's bits,
    ``+`` adds, ``-`` removes; an empty who is umask-masked (022), so
    it grants no write to group/other; copy letters (``o=u``) grant
    the source class's *current* bits. ``s``/``t`` grant no write and
    are ignored; ``X`` on a directory behaves as ``x``. Returns None
    for a mode that is not symbolic at all (``777``, ``--help``).
    """
    perms: dict[str, set[str]] = {"u": {"r", "w", "x"},
                                  "g": {"r", "x"}, "o": {"r", "x"}}
    for clause in mode.split(","):
        m = _SYMBOLIC_CLAUSE.match(clause)
        if not m:
            return None
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
                granted.update(perms["u"])
            elif ch == "g":
                granted.update(perms["g"])
            elif ch == "o":
                granted.update(perms["o"])
            # "s"/"t": setuid/setgid/sticky — no write grant, ignored
        for c in classes:
            grant = set(granted)
            if masked and c in "go":
                grant.discard("w")
            if op == "=":
                perms[c] = grant
            elif op == "+":
                perms[c] |= grant
            else:  # "-": removal can only take bits away
                perms[c] -= grant
    return perms


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
    (``o=u``) grant the source class's current bits — owner root has
    rwx, group root has r-x. An empty who (``+rwx``) is umask-masked
    in the shell, so with the standard 022 umask other gains r-x but
    not w, and it stays allowed, exactly like the allowed ``755 /``.
    ``s``/``t`` grant no write and are ignored; ``X`` on a directory
    behaves as ``x``. A non-symbolic mode (``777``, ``--help``)
    returns False: the numeric pattern owns it.
    """
    perms = _chmod_symbolic_dir_perms(mode)
    return perms is not None and "w" in perms["o"]


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


def _target_is_home(tok: str) -> bool:
    """True if *tok* names a home directory (literal, brace or glob
    spelling, or a symlink landing on one).

    Used by the chmod mode check below: ``700``/``750`` on a home is
    the standard secure setting, not a lockout — the same mode on a
    shared system tree locks every other account out of it, so the
    two need telling apart. A glob that can match a home counts as a
    home (``chmod 700 /home/*`` secures every home); anything that
    cannot be classified as a home is priced as a system tree.
    """
    t = tok.strip("\"'")
    if not t:
        return False
    homes = _home_dirs()
    for expansion in _expand_braces(t):
        spell = _normalize_path_spelling(expansion)
        if not spell:
            continue
        base = spell.rstrip("/") or "/"
        if base in homes:
            return True
        if _has_glob(spell) and any(
                fnmatch.fnmatchcase(h, spell) for h in homes):
            return True
    resolved = _realpath_of(tok)
    return resolved is not None and resolved in homes


def _chmod_mode_nukes_dir(mode: str, target: str) -> bool:
    """True if chmod *mode* wrecks guarded dir *target* in one shot.

    Three end states count as a nuke, judged on the mode's last three
    digits (numeric) or its evaluated end state (symbolic, from the
    directory's usual 755 start):

    - world-writable: other gains ``w`` (``777``, ``a+rwx``, ``o+w``)
      — the already-blocked ``/`` case, now for every guarded dir;
    - owner locked out: the owner class ends without ``x``
      (``000``, ``644``, ``u-x``) — the owner cannot traverse their
      own directory any more, so the tree is bricked for them too;
    - others locked out of a shared tree: other ends without ``x``
      (``700``, ``750``, ``o-x``) on a system tree — every other
      account loses the tree. The same mode on a *home* is the
      standard secure setting (``700 ~``), so homes are exempt from
      this leg only.

    ``755``/``751``/``u+rwx``/``o+r`` and the other harmless modes
    stay allowed, as does any mode on a non-guarded dir. A mode that
    is neither numeric nor symbolic (``--help``) is not a nuke.
    """
    home = _target_is_home(target)
    if re.fullmatch(r"[0-7]{1,4}", mode):
        digits = mode[-3:].rjust(3, "0")
        u, _g, o = (int(d) for d in digits)
        if o & 0b010:
            return True  # world-writable
        if not u & 0b001:
            return True  # owner locked out of their own dir
        if not home and not o & 0b001:
            return True  # others locked out of a shared tree
        return False
    perms = _chmod_symbolic_dir_perms(mode)
    if perms is None:
        return False
    if "w" in perms["o"]:
        return True
    if "x" not in perms["u"]:
        return True
    if not home and "x" not in perms["o"]:
        return True
    return False


def _tree_permission_reason(tokens: list[str]) -> str | None:
    """``chown``/``chgrp``/``chmod``/``chattr`` rewriting a guarded tree.

    The blocklist already refused a recursive delete of ``/``, a home
    or a named system tree, and a permission nuke of ``/`` itself —
    but the same destruction spelled as an ownership or permission
    change of any other guarded dir sailed through (verified against
    ``_is_dangerous`` before this fix):

    - ``chown -R nobody /etc`` / ``chgrp -R nogroup /`` re-own a
      whole system tree (the audit's hardening note 1 named
      ``chown -R nobody /`` explicitly); ``chmod -R 755 /`` rewrites
      every mode in the tree — private keys world-readable, setuid
      bits stripped — and ``chmod -R 777 /etc`` / ``-R 000 /var``
      open or lock a whole tree the same way. Any recursive
      chown/chgrp/chmod of a guarded dir is refused, whatever the
      mode or owner: the tree-wide rewrite is the damage.
    - A single (non-recursive) chmod of a guarded dir with a nuke
      mode — world-writable (``777``, ``a+rwx``), owner locked out
      (``000``, ``644``), or others locked out of a shared tree
      (``700``/``750``/``o-x`` on ``/etc``) — is refused the same
      way ``chmod 777 /`` already was.
    - ``chattr +i`` on a guarded dir makes the tree immutable —
      nothing inside it can ever change again — so it is refused
      too, recursive or not (``-i`` only removes the flag, so it
      stays allowed).

    Precision follows the rm guard exactly: subdirectories stay the
    caller's responsibility (``chmod -R 777 /etc/myapp``,
    ``chown -R www-data /var/www/site``, ``chattr +i /tmp/x``),
    non-recursive single-file chown/chmod (``chown root /etc/passwd``,
    ``chmod 600 ~/.ssh/id_rsa``) and harmless modes (``755``,
    ``700 ~``) stay allowed. Verbs are matched by basename, so
    ``/bin/chmod`` and ``sudo chown`` are caught too; ``--reference``
    modes are unpriceable, so only the recursive rule applies there.
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        if name not in ("chown", "chgrp", "chmod", "chattr"):
            continue
        args = _args_until_separator(tokens, i + 1)
        # GNU tools permute options, so -R/--recursive counts wherever
        # it appears before the -- terminator — not just ahead of the
        # mode/owner (``chmod 755 -R /etc`` recurses like ``-R`` first).
        recursive = False
        for arg in args:
            if arg == "--":
                break
            if arg == "--recursive":
                recursive = True
                break
            if (arg.startswith("-") and arg != "-"
                    and not arg.startswith("--") and "R" in arg[1:]):
                recursive = True
                break
        if name in ("chown", "chgrp"):
            j = 0
            while j < len(args):
                arg = args[j]
                if arg == "--":
                    j += 1
                    break
                if arg.startswith("--"):
                    if arg in ("--from", "--reference"):
                        j += 1  # skip the option's value
                    j += 1
                    continue
                if arg.startswith("-") and arg != "-":
                    j += 1
                    continue
                break
            operands = args[j:]
            if operands:
                operands = operands[1:]  # owner (chown) / group (chgrp)
            if recursive and any(
                    _is_root_or_home(t) or _realpath_is_guarded(t)
                    for t in operands):
                return "recursive ownership change of a guarded directory"
            continue
        if name == "chmod":
            reference = False
            j = 0
            while j < len(args):
                arg = args[j]
                if arg == "--":
                    j += 1
                    break
                if arg.startswith("--"):
                    if arg == "--reference":
                        reference = True
                        j += 1  # skip the option's value
                    j += 1
                    continue
                if arg.startswith("-") and arg != "-":
                    j += 1
                    continue
                break
            if j >= len(args):
                continue
            if reference:
                # Mode comes from the reference file: unpriceable
                # here, so only the tree-wide rewrite is priced.
                if recursive and any(
                        _is_root_or_home(t) or _realpath_is_guarded(t)
                        for t in args[j:]):
                    return "recursive permission change of a guarded directory"
                continue
            mode = args[j]
            targets = [t for t in args[j + 1:]
                       if not t.startswith("-")]
            if not targets:
                continue
            if recursive and any(
                    _is_root_or_home(t) or _realpath_is_guarded(t)
                    for t in targets):
                return "recursive permission change of a guarded directory"
            for target in targets:
                if (_is_root_or_home(target)
                        or _realpath_is_guarded(target)) \
                        and _chmod_mode_nukes_dir(mode, target):
                    return "permission nuke of a guarded directory"
            continue
        # chattr: the op is the first operand (+i / -i / =i); -p/-v
        # take values, the rest are flags. Only adding the immutable
        # bit locks a tree, and only a guarded dir counts as one.
        operands, _shorts, _longs, _vals = _split_tool_args(
            args, value_shorts=frozenset("pv"))
        if not operands:
            continue
        op = operands[0]
        if len(op) < 2 or op[0] not in "+=" or "i" not in op[1:]:
            continue
        if any(_is_root_or_home(t) or _realpath_is_guarded(t)
               for t in operands[1:]):
            return "immutable flag on a guarded directory"
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
# ``_is_dangerous`` before this fix). The same gap held for the MTD
# flash devices (``/dev/mtd0``, ``mtdblock0``, the UBI nodes
# ``ubi0`` / ``ubi0_0`` / ``ubiblock0_0``), the s390 DASD disks
# (``dasda``) and the Xen virtual disks (``xvda``): ``cp file
# /dev/mtd0`` and ``tee /dev/xvda`` overwrote the flash / disk
# exactly like ``cp`` to ``/dev/sda``, but matched no name in the
# list. The read-only MTD nodes (``mtd0ro``) are deliberately not
# matched: the ``$`` anchor leaves the ``ro`` suffix unmatched, so
# a write aimed at one — which the device would refuse anyway —
# stays allowed. The stable symlinks under ``/dev/disk/by-*/`` and
# the mapper nodes are covered separately in
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
    r"|mtd[0-9]+"
    r"|mtdblock[0-9]+"
    r"|ubi[0-9]+(?:_[0-9]+)?"
    r"|ubiblock[0-9]+(?:_[0-9]+)?"
    r"|dasd[a-z]+[0-9]*"
    r"|xvd[a-z]+[0-9]*"
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
    partition (1-3); ``mtd{0..3}`` / ``mtdblock{0..3}``;
    ``ubi{0..3}`` with its volumes (``ubi0_0`` ..) and
    ``ubiblock{0..3}_0``; ``dasd{a..d}`` / ``xvd{a..d}`` with an
    optional partition (1-3). Built lazily and cached: a glob pattern
    is only ever matched against this list, never against the live
    ``/dev`` — the check must be deterministic in tests and must not
    depend on which devices this machine happens to have.
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
        for i in range(4):
            names.append(f"mtd{i}")
            names.append(f"mtdblock{i}")
            names.append(f"ubi{i}")
            names.append(f"ubiblock{i}_0")
            for vol in range(3):
                names.append(f"ubi{i}_{vol}")
        for base in ("dasda", "dasdb", "dasdc", "dasdd",
                     "xvda", "xvdb", "xvdc", "xvdd"):
            names.append(base)
            for part in range(1, 4):
                names.append(f"{base}{part}")
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


def _is_device_target(tok: str) -> bool:
    """A raw-device spelling, or a symlink spelling landing on one."""
    return _is_raw_device_spelling(tok) or _realpath_is_raw_device(tok)


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

    The same write spelled through a download or copy tool: ``curl
    -o`` / ``--output`` and ``wget -O`` / ``--output-document`` name
    the file the download lands in, so ``curl -o /dev/sda <url>``
    writes the download straight onto the disk exactly like ``cp``
    to it — but neither matched any pattern or token check, since
    the scan only knew the plain copy spellings (verified against
    ``_is_dangerous`` before this fix); the same hole stayed open
    for ``rsync`` / ``scp``, whose destination is a plain positional
    operand exactly like ``cp``'s — ``rsync /dev/zero /dev/sda``
    and ``scp /tmp/img /dev/sda`` overwrite the disk the same way.
    Only the output target counts: a device as a source (``rsync
    /dev/sda /tmp/backup``, ``scp /dev/sda host:/tmp/x``) stays
    allowed — reading it is the point — as do remote destinations
    (``scp /tmp/img host:/dev/sda`` names a path on the host, not
    the local disk) and the plain forms (``curl <url>`` writes to
    stdout, ``wget -O-`` the same). ``wget``'s log outputs (``-o`` /
    ``--output-file``, ``-a`` / ``--append-output``) and ``curl``'s
    header dump (``-D`` / ``--dump-header``) are file outputs too,
    so a device there blocks as well; ``curl -O`` only names the
    file after the URL inside the working directory, so it stays
    allowed.
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        if name not in ("cp", "mv", "tee", "install",
                        "curl", "wget", "rsync", "scp"):
            continue
        args = _args_until_separator(tokens, i + 1)
        if name == "curl":
            # Only the file-output values count: -o/--output is the
            # download's file, -D/--dump-header writes the response
            # headers to a file. The other value-taking shorts are
            # declared too (-d data, -H header, -u user, -A agent,
            # -e referer, -T upload file, -F form, -K config,
            # -w write-out, -m max time, -y/-Y speed limits, -E
            # cert, -c cookie jar, -b cookie, -x proxy, -U proxy
            # user, -r range), so their values are consumed as
            # values and never mistaken for an output file — a
            # device spelling as POST data (``-d /dev/sda``) is
            # just a string sent to the server, not a write.
            _operands, _shorts, _longs, values = _split_tool_args(
                args,
                value_shorts=frozenset("oDdHuAeTFKwmyYEcbxUr"),
                value_longs=frozenset(("output", "dump-header")))
            for flag, val in values:
                if flag in ("o", "output", "D", "dump-header") \
                        and val and _is_device_target(val):
                    return "raw disk write"
            continue
        if name == "wget":
            # -O/--output-document is the download's file; -o/
            # --output-file writes the log to a file and -a/
            # --append-output appends the log to one — all three
            # are file outputs, so a device in any of them blocks.
            # -O- / -o- mean stdout, so "-" never matches.
            _operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("Ooa"),
                value_longs=frozenset(
                    ("output-document", "output-file",
                     "append-output")))
            for flag, val in values:
                if flag in ("O", "output-document", "o", "output-file",
                            "a", "append-output") \
                        and val and _is_device_target(val):
                    return "raw disk write"
            continue
        if name == "rsync":
            # Destination is the last operand, exactly like cp.
            # Value options per the rsync man page (-e rsh, and the
            # value-taking longs), so their values are never
            # mistaken for the destination. --log-file is a file
            # output too, so a device there blocks as well.
            operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("e"),
                value_longs=frozenset(
                    ("rsh", "rsync-path", "log-file", "log-file-format",
                     "password-file", "backup-dir", "suffix",
                     "partial-dir", "temp-dir", "address", "port",
                     "timeout", "bwlimit", "max-size", "min-size",
                     "outformat")))
            candidates = ([operands[-1]] if operands else [])
            candidates += [v for flag, v in values
                           if flag == "log-file"]
            for dest in candidates:
                if dest and _is_device_target(dest):
                    return "raw disk write"
            continue
        if name == "scp":
            # Destination is the last operand, exactly like cp.
            # Value options per the scp man page (-P port, -c
            # cipher, -F config, -i identity, -l limit, -o ssh
            # option, -S program), so their values are never
            # mistaken for the destination.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("PcFiloS"),
                value_longs=frozenset())
            if operands and _is_device_target(operands[-1]):
                return "raw disk write"
            continue
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
            if dest and _is_device_target(dest):
                return "raw disk write"
    return None


# Commands whose entire purpose is overwriting or erasing their target:
# ``shred`` writes over it, ``wipefs`` strips its filesystem signatures,
# ``blkdiscard`` discards its blocks, ``wipe`` overwrites it securely
# and ``srm`` is the secure-delete ``rm``. Aimed at a raw disk or memory
# device, each destroys it exactly like the ``dd``/``cp`` writes above
# — but ``wipe``/``srm`` matched no pattern or token check (verified
# against ``_is_dangerous`` before this fix). Only raw-device targets
# count: ``shred /tmp/file`` and ``wipefs /dev/null`` stay allowed, as
# does every flag spelling (value words such as ``shred -n 1``'s ``1``
# are harmless operands that never match the device pattern).
def _device_wipe_reason(tokens: list[str]) -> str | None:
    """``shred``/``wipefs``/``blkdiscard``/``wipe``/``srm`` aimed at a raw device."""
    for i, tok in enumerate(tokens):
        if _basename(tok) not in ("shred", "wipefs", "blkdiscard",
                                  "wipe", "srm"):
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
            if target and _is_device_target(target):
                return "raw disk wipe"
    return None


# Archive tools writing their output archive onto a raw device:
# ``tar -cf`` writes the archive to the ``-f`` file, so ``tar -cf
# /dev/sda /tmp/foo`` overwrites the disk with a tar stream exactly
# like ``cp`` to it — but no pattern or token check knew the archive
# tools at all (verified against ``_is_dangerous`` before this fix).
# The same write spelled through the other archivers: ``zip /dev/sda
# file`` writes the zip archive onto the disk (the archive is the
# first operand); ``7z a /dev/sda file`` writes it under the add
# subcommand (``7za`` / ``7zr`` are the same tool); ``cpio -o -F
# /dev/sda`` writes the copy-out archive to the ``-F`` file;
# ``mksquashfs /tmp/foo /dev/sda`` writes the squashfs image to its
# filesystem operand; ``genisoimage -o /dev/sda dir`` (``mkisofs``
# is the same tool) writes the ISO image to the ``-o`` file. Only the
# output archive counts, so every read form stays allowed: ``tar
# -tf`` / ``-xf`` / ``-df`` list, extract or compare the archive
# (reading a device is the point), ``zip -T`` tests it, ``7z x`` /
# ``l`` / ``t`` extract, list or test it, and ``cpio -i`` reads it —
# as does a device named as an input (``tar -cf /tmp/x.tar /dev/sda``
# archives the device, ``mksquashfs /dev/sda /tmp/x.sqfs`` squashes
# it). ``tar``'s ``-g`` snapshot file is a write too, so a device
# there blocks in a write mode, and ``genisoimage``'s ``-log-file``
# is a file output the same way ``wget -o`` / ``rsync --log-file``
# are. Value options are consumed so their values are never mistaken
# for the archive (``zip -s``, ``7z -o``, ``tar -C``), and
# ``mksquashfs`` takes its filesystem image as the last positional
# before the options, so a device named only as a source stays
# allowed.
def _archive_device_reason(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name == "tar":
            # -f/--file is the archive; -g/--listed-incremental is
            # the snapshot file tar writes during a create. Only the
            # write modes (create, append, update, concatenate,
            # delete) write the archive: -t lists it, -x extracts
            # it, -d compares it — all reads. Value options per tar
            # --help (-F info script, -T files-from, -C directory,
            # -V label, -X exclude file, -K starting file, -N newer
            # date, -L tape length, -b blocking factor, -g snapshot
            # file) and their long forms, so their values are never
            # mistaken for the archive.
            _operands, shorts, longs, values = _split_tool_args(
                args, value_shorts=frozenset("fFTCVXKNLbg"),
                value_longs=frozenset(
                    ("file", "label", "exclude", "files-from",
                     "info-script", "newer-mtime", "newer",
                     "after-date", "transform", "xform", "directory",
                     "listed-incremental", "tape-length",
                     "blocking-factor", "starting-file",
                     "exclude-from", "occurrence", "level", "sort",
                     "format", "index-file")))
            if not (shorts & {"c", "r", "u", "A"} or longs & {
                    "create", "append", "update", "concatenate",
                    "catenate", "delete"}):
                continue
            for flag, val in values:
                if flag in ("f", "file", "g", "listed-incremental") \
                        and val and _is_raw_device_spelling(val):
                    return "raw disk write"
        elif name == "zip":
            # The archive is the first operand. -T/--test only tests
            # it (read-only); -h/--help and --version only print.
            # Value options per zip --help (-b temp path, -P
            # password, -s split size, -t date, -n suffixes), so
            # their values are never mistaken for the archive.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("bPstn"))
            if shorts & {"T", "h"} or longs & {
                    "test", "help", "version"}:
                continue
            if operands and _is_raw_device_spelling(operands[0]):
                return "raw disk write"
        elif name in ("7z", "7za", "7zr"):
            # Subcommand first, archive second: only the write
            # subcommands (a add, d delete, u update, rn rename)
            # write the archive — x/e extract it, l lists it, t
            # tests it. Value options per 7z --help (-o output dir,
            # -p password, -m method, -t type, -w working dir, -v
            # volume size), so their values are never mistaken for
            # the archive.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("opmtwv"))
            if len(operands) < 2 or operands[0] not in (
                    "a", "d", "u", "rn"):
                continue
            if _is_raw_device_spelling(operands[1]):
                return "raw disk write"
        elif name == "cpio":
            # -F/--file is the archive, but only in copy-out mode:
            # -o writes it, -i reads it, -p passes files through
            # with no archive at all.
            _operands, shorts, longs, values = _split_tool_args(
                args, value_shorts=frozenset("F"),
                value_longs=frozenset(("file",)))
            if "o" not in shorts and "create" not in longs:
                continue
            for flag, val in values:
                if flag in ("F", "file") and val \
                        and _is_raw_device_spelling(val):
                    return "raw disk write"
        elif name == "mksquashfs":
            # Sources, then the filesystem image, then options: the
            # image is the last positional before the first option
            # token, so a device named only as a source stays
            # allowed. -- ends option parsing; everything after it
            # is positional.
            positionals: list[str] = []
            j = 0
            while j < len(args):
                arg = args[j]
                if arg == "--":
                    positionals.extend(args[j + 1:])
                    break
                if arg.startswith("-") and arg != "-":
                    break
                positionals.append(arg)
                j += 1
            if len(positionals) >= 2 and _is_raw_device_spelling(
                    positionals[-1]):
                return "raw disk write"
        elif name in ("genisoimage", "mkisofs"):
            # -o/--output is the ISO file. -log-file is a file
            # output too, the same way wget -o / rsync --log-file
            # are — but it is a single-dash long option, so the
            # cluster parse reads its 'o' as the -o value flag and
            # its value lands as an operand; check it by hand.
            for j, arg in enumerate(args):
                if arg in ("-log-file", "--log-file") \
                        and j + 1 < len(args) \
                        and _is_raw_device_spelling(args[j + 1]):
                    return "raw disk write"
                if arg.startswith("--log-file=") \
                        and _is_raw_device_spelling(
                            arg.split("=", 1)[1]):
                    return "raw disk write"
            _operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("o"),
                value_longs=frozenset(("output",)))
            for flag, val in values:
                if flag in ("o", "output") and val \
                        and _is_raw_device_spelling(val):
                    return "raw disk write"
    return None


# Disk-image tools writing their output image onto a raw device:
# ``qemu-img convert`` is the standard way to write a VM image onto
# a physical disk — ``qemu-img convert -O raw image.qcow2 /dev/sda``
# streams the whole image onto the disk exactly like ``dd`` to it —
# but no pattern or token check knew the image tools at all (verified
# against ``_is_dangerous`` before this fix). The same write spelled
# through the other image tools: ``qemu-img create -f raw /dev/sda
# 10G`` (the image file is created on the device), ``VBoxManage
# convertfromraw image.raw /dev/sda`` and ``VBoxManage clonemedium
# disk image.vdi /dev/sda`` (the output medium is the last operand),
# ``virt-sparsify image.qcow2 /dev/sda`` and ``virt-resize
# image.qcow2 /dev/sda`` (the output disk is the last operand). Only
# the output image counts, so every read form stays allowed
# (``qemu-img info`` / ``check`` / ``compare`` / ``map`` /
# ``measure``, ``VBoxManage showmediuminfo``, a device named as an
# input — ``qemu-img convert /dev/sda /tmp/x.img`` reads the disk
# into a file, ``virt-sparsify /dev/sda /tmp/out.qcow2`` sparsifies
# it into one), as does a device spelling as another option's value
# (``qemu-img convert -B /dev/sda`` names the backing file,
# ``qemu-img create -b /dev/sda`` the same, ``virt-resize --expand
# /dev/sda1`` the partition to grow). ``qemu-img resize`` only
# shrinks with ``--shrink`` (without it a shrink is refused), so
# only that spelling blocks, and ``virt-sparsify --in-place``
# rewrites its single operand in place, so a raw-device operand
# blocks there too. With ``--image-opts`` / ``--target-image-opts``
# the output file is spelled as ``filename=`` inside the option
# string instead of a plain positional
# (``driver=file,filename=/dev/sda``), so that spelling is checked
# too — and so is the JSON form (``{"filename": "/dev/sda"}``).
def _is_image_target(tok: str) -> bool:
    if _is_raw_device_spelling(tok):
        return True
    for part in tok.split(","):
        if "filename=" in part:
            if _is_raw_device_spelling(
                    part.split("filename=", 1)[1]):
                return True
    match = re.search(r'"filename"\s*:\s*"([^"]+)"', tok)
    if match and _is_raw_device_spelling(match.group(1)):
        return True
    return False


def _image_device_reason(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name == "qemu-img":
            # Value options per qemu-img --help (-f input format,
            # -O output format, -B backing file (convert) / -b
            # backing file (create), -F backing format, -o options,
            # -l snapshot name, -s snapshot id, -t cache mode, -T
            # source cache mode, -S sparse size, -r rate limit, -m
            # coroutines) and their long forms (--preallocation
            # takes a value; --image-opts / --target-image-opts /
            # --force-share are plain flags), so their values are
            # never mistaken for the output image.
            # ``qemu-img dd`` is already caught by the generic
            # ``dd of=`` pattern, so it needs no branch here.
            operands, _shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("fOBFbolstTSrm"),
                value_longs=frozenset(("object", "preallocation")))
            if not operands:
                continue
            subcommand = operands[0]
            if subcommand == "convert":
                # infile, then outfile: only the outfile is a write.
                if len(operands) >= 3 and _is_image_target(
                        operands[-1]):
                    return "raw disk write"
            elif subcommand == "create":
                # filename, then size: the filename is the write.
                if len(operands) >= 2 and _is_image_target(
                        operands[1]):
                    return "raw disk write"
            elif subcommand == "resize":
                # Only --shrink can shrink: without it qemu-img
                # refuses to shrink, and a grow writes nothing
                # past the old end.
                if "shrink" in longs and len(operands) >= 2 and \
                        _is_raw_device_spelling(operands[1]):
                    return "raw disk write"
        elif name == "VBoxManage":
            # Value options per VBoxManage --help (--format,
            # --variant, --existing), so their values are never
            # mistaken for the output medium.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_longs=frozenset(
                    ("format", "variant", "existing")))
            if not operands:
                continue
            subcommand = operands[0]
            if subcommand == "convertfromraw":
                # infile, then outfile.
                if len(operands) >= 3 and _is_raw_device_spelling(
                        operands[2]):
                    return "raw disk write"
            elif subcommand in ("clonemedium", "clonehd"):
                # [disk|dvd|floppy] src dst: the destination medium
                # is the last operand.
                rest = operands[1:]
                if rest and rest[0] in ("disk", "dvd", "floppy"):
                    rest = rest[1:]
                if len(rest) >= 2 and _is_raw_device_spelling(
                        rest[-1]):
                    return "raw disk write"
        elif name in ("virt-sparsify", "virt-resize"):
            # Value options per virt-sparsify / virt-resize --help
            # (--format, --convert, --compress, --tmp,
            # --check-tmpdir, --output-format, and virt-resize's
            # --expand / --shrink / --resize / --delete / --add,
            # which name a partition or a file), so their values
            # are never mistaken for the output disk.
            operands, _shorts, longs, _vals = _split_tool_args(
                args, value_longs=frozenset(
                    ("format", "convert", "compress", "tmp",
                     "check-tmpdir", "output-format", "expand",
                     "shrink", "resize", "resize-force",
                     "unknown-filesystems", "lv-expand", "delete",
                     "add")))
            if not operands:
                continue
            if "in-place" in longs:
                # --in-place rewrites the single operand in place.
                if any(_is_raw_device_spelling(o) for o in operands):
                    return "raw disk write"
                continue
            # indisk, then outdisk: only the outdisk is a write.
            if len(operands) >= 2 and _is_image_target(operands[-1]):
                return "raw disk write"
    return None


# An MTD flash device node, for the flash-tool check below: the
# ``mtd`` / ``mtdblock`` / ``ubi`` / ``ubiblock`` alternatives of
# ``_RAW_DEVICE``. Kept separate because the flash tools only ever
# act on MTD devices — ``flash_erase /dev/sda`` is a certain failure
# (the tool refuses a non-MTD device), not a footgun, so it stays
# allowed while ``flash_erase /dev/mtd0`` blocks. The read-only
# nodes (``mtd0ro``) are not matched, the same way ``_RAW_DEVICE``
# leaves them unmatched, so a dump or erase aimed at one stays
# allowed. Glob spellings are checked against the representative
# names, the way ``_is_raw_device_spelling`` does.
_MTD_DEVICE = re.compile(
    r"^/dev/(?:mtd[0-9]+|mtdblock[0-9]+"
    r"|ubi[0-9]+(?:_[0-9]+)?|ubiblock[0-9]+(?:_[0-9]+)?)$")


def _is_mtd_device_spelling(tok: str) -> bool:
    """True if *tok* names an MTD flash device (or UBI volume) node."""
    mtd_names = [n for n in _raw_device_names()
                 if n.startswith(("mtd", "ubi"))]
    for expansion in _expand_braces(tok):
        spell = _normalize_device_spelling(expansion)
        if not spell:
            continue
        if _MTD_DEVICE.match(spell):
            return True
        segments = [s for s in spell.split("/") if s != ""]
        if len(segments) == 2 and fnmatch.fnmatchcase("dev", segments[0]):
            name = segments[1]
            if _has_glob(name) and any(
                    fnmatch.fnmatchcase(n, name) for n in mtd_names):
                return True
    return False


_UBI_NODE = re.compile(r"^/dev/ubi[0-9]+(?:_[0-9]+)?$")


def _is_ubi_node_spelling(tok: str) -> bool:
    """True if *tok* names a UBI device or volume node (``/dev/ubi0``,
    ``/dev/ubi0_0``) after shell expansion.

    The UBI volume tools take a UBI node, not an MTD node: the
    ``ubiblock`` read-only block emulation and the ``mtdNro`` nodes
    are not volume nodes, so neither counts — ``ubiupdatevol
    /dev/ubiblock0_0`` and ``ubirmvol /dev/mtd0`` both fail in the
    real tools. Glob spellings are checked against the representative
    UBI names, the same way ``_is_mtd_device_spelling`` does.
    """
    ubi_names = [n for n in _raw_device_names()
                 if n.startswith("ubi") and not n.startswith("ubiblock")]
    for expansion in _expand_braces(tok):
        spell = _normalize_device_spelling(expansion)
        if not spell:
            continue
        if _UBI_NODE.match(spell):
            return True
        segments = [s for s in spell.split("/") if s != ""]
        if len(segments) == 2 and fnmatch.fnmatchcase("dev", segments[0]):
            name = segments[1]
            if _has_glob(name) and any(
                    fnmatch.fnmatchcase(n, name) for n in ubi_names):
                return True
    return False


def _mtd_device_reason(tokens: list[str]) -> str | None:
    """MTD flash tools erasing or writing a flash device.

    ``flash_erase /dev/mtd0 0 5`` erases the flash partition exactly
    like ``shred`` on a disk — but no pattern or token check knew the
    mtd-utils flash tools at all (verified against ``_is_dangerous``
    before this fix). The same write spelled through the other flash
    tools: ``flash_eraseall`` is the same tool under its old name;
    ``nandwrite /dev/mtd0 image.bin`` writes the image onto the
    device (the device is the first operand); ``flashcp image.bin
    /dev/mtd0`` copies the file onto it (the device is the last
    operand); ``ubiformat /dev/mtd0`` formats it for UBI (the device
    is the first operand); ``mtd_debug erase`` / ``write`` erase or
    write it (the subcommand is the first operand, the device the
    second). Only the flash device counts, so every read form stays
    allowed: ``nanddump`` dumps the device, ``mtdinfo`` /
    ``flash_info`` describe it, and ``mtd_debug info`` / ``read``
    read it — as does a device named only as another option's value
    (``nandwrite -s`` is the start address, ``ubiformat -f`` the
    image file, ``mtd_debug write``'s image file is its last
    operand), and a non-MTD device (``flash_erase /dev/sda`` fails:
    the tool refuses anything but an MTD device). Value options are
    consumed so their values are never mistaken for the device
    (``nandwrite -s``, ``ubiformat -f`` / ``-s`` / ``-O``).
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name in ("flash_erase", "flash_eraseall"):
            # [options] <mtd-device> <start-block> <block-count>:
            # the device is the first operand.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            if operands and _is_mtd_device_spelling(operands[0]):
                return "flash erase"
        elif name == "nandwrite":
            # Value options per nandwrite --help (-s start address),
            # so the address is never mistaken for the device.
            # [options] <mtd-device> <input-file>: the device is the
            # first operand.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("s"))
            if operands and _is_mtd_device_spelling(operands[0]):
                return "flash write"
        elif name == "flashcp":
            # [options] <filename> <mtd-device>: the device is the
            # last operand.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            if operands and _is_mtd_device_spelling(operands[-1]):
                return "flash write"
        elif name == "ubiformat":
            # Value options per ubiformat --help (-f image file, -s
            # sub-page size, -O VID header offset), so their values
            # are never mistaken for the device.
            # [options] <mtd-device>: the device is the first operand.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("sfO"))
            if operands and _is_mtd_device_spelling(operands[0]):
                return "flash write"
        elif name == "mtd_debug":
            # <info|read|write|erase> <mtd-device> ...: only the
            # write / erase subcommands touch the device.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            if len(operands) >= 2 and operands[0] in ("write", "erase") \
                    and _is_mtd_device_spelling(operands[1]):
                return ("flash erase" if operands[0] == "erase"
                        else "flash write")
    return None


def _ubi_device_reason(tokens: list[str]) -> str | None:
    """UBI volume tools writing to, removing, or tearing down a UBI
    volume or device.

    The flash tools covered above stop at the MTD layer, but the UBI
    layer on top of it destroys the same flash data through its own
    tools — and no pattern or token check knew any of them (verified
    against ``_is_dangerous`` before this fix). ``ubiupdatevol
    /dev/ubi0_0 /tmp/image.bin`` writes the image into the volume,
    overwriting whatever was there (the volume is the first operand;
    with no image file it reads stdin, and ``-t`` truncates the
    volume to zero); ``ubirmvol /dev/ubi0 -N rootfs`` removes the
    volume and its data (the device is the first operand, ``-N`` /
    ``-n`` name it); ``ubimkvol /dev/ubi0 -N data -s 10MiB`` creates
    a volume on the device, rewriting its volume table (the device
    is the first operand; ``-m`` uses the maximum available size,
    so it is a flag, not a value option); ``ubirsvol /dev/ubi0_0
    -s 10MiB`` resizes the volume, and an absolute size (or a
    leading ``-`` one) shrinks it, discarding every block past the
    new end — the same data loss as ``lvresize``; ``ubidetach -m
    0`` tears the whole UBI device (and its volumes) down, so every
    real use blocks, whatever spelling names the target (``-m`` the
    MTD number, ``-p`` its path, ``-d`` the UBI number, or the UBI
    device as a positional); ``ubinize -o /dev/sda`` writes the UBI
    image it builds onto the disk exactly like ``genisoimage -o``
    already blocked. Only the UBI node counts, so every read form
    stays allowed: ``ubiattach`` only scans the flash and creates
    the nodes, ``ubinfo`` / ``ubiinfo`` describe them, ``ubirsvol
    -s +10MiB`` only grows the volume (the ``lvresize`` rule: with
    no size given the command errors, so it stays allowed too), a
    bare ``ubidetach`` errors the same way, ``ubinize -o`` to a
    regular file is the normal build, and the read-only
    ``ubiblock`` nodes (``ubiupdatevol /dev/ubiblock0_0``) and a
    plain MTD node (``ubirmvol /dev/mtd0``) are refused by the real
    tools. Value options are consumed so their values are never
    mistaken for the node (``ubimkvol -N`` / ``-s`` / ``-t`` /
    ``-a``, ``ubirmvol -N`` / ``-n``, ``ubinize -o`` / ``-p`` /
    ``-m`` / ``-s`` / ``-O``).
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name == "ubiupdatevol":
            # [options] <volume-node> [<image-file>]: the volume is
            # the first operand; the image file is read, so only the
            # first operand counts. Value options per ubiupdatevol
            # --help (-s size); -t truncates the volume.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("s"),
                value_longs=frozenset(("size",)))
            if operands and _is_ubi_node_spelling(operands[0]):
                return "UBI volume write"
        elif name == "ubirmvol":
            # [options] <ubi-device>: the device is the first
            # operand. Value options per ubirmvol --help (-N name,
            # -n volume id).
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("Nn"),
                value_longs=frozenset(("name", "vol_id")))
            if operands and _is_ubi_node_spelling(operands[0]):
                return "UBI volume remove"
        elif name == "ubimkvol":
            # [options] <ubi-device>: the device is the first
            # operand. Value options per ubimkvol --help (-N name,
            # -s size, -t type, -a alignment); -m is the
            # max-available-size flag, so it takes no value and a
            # device after it stays an operand.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("Nsta"),
                value_longs=frozenset(
                    ("name", "size", "type", "alignment")))
            if operands and _is_ubi_node_spelling(operands[0]):
                return "UBI volume create"
        elif name == "ubirsvol":
            # [options] <volume-node> -s <size>: the volume is the
            # first operand. A leading + grows the volume and
            # destroys nothing; an absolute size or a leading -
            # shrinks it — the lvresize rule, including staying
            # allowed with no size given (the command errors).
            operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("s"),
                value_longs=frozenset(("size",)))
            if not operands or not _is_ubi_node_spelling(operands[0]):
                continue
            for flag, val in values:
                if flag in ("s", "size") and not val.startswith("+"):
                    return "UBI volume resize"
        elif name == "ubidetach":
            # -m <mtd-num> / -p <path> / -d <ubi-num> / <ubi-device>:
            # every real use tears the UBI device down. Value
            # options per ubidetach --help (-m MTD number, -p MTD
            # path, -d UBI number); a bare invocation errors out,
            # so it stays allowed, like bare sg_write_same.
            operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("mpd"),
                value_longs=frozenset(
                    ("mtdnum", "dev-path", "devn")))
            if operands or values:
                return "UBI detach"
        elif name == "ubinize":
            # -o <output-file> is the UBI image: writing it onto a
            # raw device overwrites the disk. Value options per
            # ubinize --help (-o output, -p peb size, -m min io
            # size, -s sub-page size, -O VID header offset); only
            # the -o value counts, the rest are never mistaken for
            # it.
            _operands, _shorts, _longs, values = _split_tool_args(
                args, value_shorts=frozenset("ompseO"),
                value_longs=frozenset(
                    ("output", "peb-size", "min-io-size",
                     "sub-page-size", "vid-hdr-offset")))
            if any(_is_raw_device_spelling(v)
                   for flag, v in values if flag in ("o", "output")):
                return "raw disk write"
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
# The same hole stayed open for the destroy forms that erase data
# without creating, formatting or partitioning anything: ``mdadm
# --zero-superblock`` (wipes a member's RAID metadata), ``zpool
# destroy`` / ``zfs destroy`` (wipe a pool / dataset) and ``zfs
# rollback`` (destroys every snapshot newer than the target), and
# ``hdparm --write-sector`` / ``--make-bad-sector`` /
# ``--trim-sector-ranges`` (write, corrupt or discard raw sectors) —
# all passed unchecked (verified against ``_is_dangerous`` before
# this fix), while their read-only forms (``mdadm --detail`` /
# ``--examine``, ``zpool status`` / ``list``, ``zfs list`` /
# ``snapshot``, ``hdparm -I`` / ``--read-sector``) and the dry runs
# (``zpool destroy -n``, ``zfs destroy -n``) stay allowed.
# The same hole stayed open for the remaining partition-table
# editors: ``gdisk`` (the GPT fdisk interactive editor — only
# ``-l`` lists and exits), ``cgdisk`` (its curses sibling, no
# read-only mode at all), ``cfdisk`` (the curses editor, which
# defaults to /dev/sda with no device given and whose --print was
# removed in util-linux 2.25) and ``fixparts`` (the GPT fdisk MBR
# repair tool, which takes no options at all) — all four open an
# editor on the raw device and write on `w`, and all four passed
# unchecked (verified against ``_is_dangerous`` before this fix),
# while their read-only forms (``gdisk -l``, ``cfdisk -r`` /
# ``--read-only``, ``cfdisk -V`` / ``-h``) stay allowed, and all of
# them stay allowed on non-device targets such as image files.
# The same hole stayed open for destroy spellings that never name a
# device path, or never matched the tool the check knew: ``lvremove
# vg0/lv0`` and ``vgremove vg0`` — the canonical LVM remove
# spellings — name a plain volume / group, so the device-target
# checks never saw them, and ``sg_format --format`` (SCSI FORMAT
# UNIT, which destroys all data on the media) and ``sg_dd of=``
# (dd under another name — the ``dd`` pattern needs a word boundary
# the underscore defeats) were unknown tools entirely — all passed
# unchecked (verified against ``_is_dangerous`` before this fix),
# while the read-only forms (bare ``sg_format``, which only prints
# the block size and count, ``sg_dd`` reading a device to a file,
# ``lvremove`` / ``vgremove --test``) stay allowed, and ``sg_format
# --format`` stays allowed on a non-device target such as an image
# file in /tmp.
# The same hole stayed open for the shrink spellings and the other
# ``dd`` twins: ``lvreduce`` exists only to shrink a logical volume
# — every block past the new end is discarded — and ``lvresize``
# with an absolute ``-L`` / ``-l`` size (or a ``-`` relative one)
# shrinks it the same way, but neither matched any check, since
# the volume is named by plain ``vg/lv`` and no device path ever
# appears; ``dcfldd`` / ``dc3dd`` (forensic ``dd`` forks) take the
# same ``if=`` / ``of=`` keywords, so ``of=/dev/sda`` writes the
# device exactly like ``dd of=/dev/sda`` — but the ``dd`` pattern
# needs a word boundary the ``dd`` in ``dcfldd`` / ``dc3dd``
# defeats; and ``ddrescue`` copies its infile to a plain
# positional outfile, so ``ddrescue /dev/sda /dev/sdb`` overwrites
# the device exactly like ``dd`` — but no ``dd`` / copy / wipe
# pattern ever saw the outfile (verified against ``_is_dangerous``
# before this fix). Precision is preserved: ``lvresize`` keeps its
# grow forms (``-L +1G``, ``-l +100``, ``--size +100%FREE``) and
# both LVM tools keep their ``--test`` dry run, ``dcfldd`` /
# ``dc3dd`` keep reading a device to a file, and ``ddrescue`` keeps
# writing its image to a regular file.
# The same hole stayed open for the remaining sg3_utils write tools
# and the other rescue twin: ``sg_write_same`` writes one block NUM
# times from LBA — ``--num=0`` writes to the end of the device, so
# one invocation can clear the whole disk (its own man page warns
# exactly that); ``sg_unmap`` discards the named blocks, the SCSI
# equivalent of the already-blocked ``blkdiscard``, and ``--all``
# unmaps to the end of the device; ``sg_write_buffer`` sends data to
# the device, and modes 4-7 download microcode, i.e. flash the
# device firmware; ``sg_write_long`` overwrites one block (plus ECC)
# at LBA, LBA 0 by default — all four passed unchecked (verified
# against ``_is_dangerous`` before this fix), while their dry runs
# (``sg_unmap`` / ``sg_write_buffer`` ``-d`` / ``--dry-run``) stay
# allowed, ``sg_write_same`` keeps its safety interlock (a bare
# invocation errors out, so it stays allowed without one of
# ``--in`` / ``--lba`` / ``--num``), and all of them stay allowed on
# non-device targets such as image files. ``dd_rescue`` (Kurt
# Garloff's, with the underscore) copies its infile to a plain
# positional outfile exactly like the ``ddrescue`` covered above,
# and in shred mode (``-2`` / ``-3`` / ``-4`` / ``-z`` / ``-Z``)
# writes random data to its single outfile operand — but its name
# matched neither the ``dd`` word boundary nor the ``ddrescue``
# check, so both spellings passed unchecked (verified against
# ``_is_dangerous`` before this fix), while reading a device to a
# regular file stays allowed, as do its log / bad-blocks outputs on
# regular files.
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
        elif name == "gdisk":
            # -l lists the partition table and exits: the man page's
            # only command-line option, and the only read-only form.
            # Anything else opens the interactive GPT editor, which
            # writes the table on `w` (the `p` / `v` menu keys are
            # interactive commands, not flags).
            operands, shorts, _longs, _vals = _split_tool_args(args)
            if "l" in shorts:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "partition table edit"
        elif name == "cgdisk":
            # The curses GPT editor: its only command-line option is
            # -a (highlighting) — there is no list or read-only mode,
            # so any invocation naming a raw device opens the editor
            # on it (and converts an MBR table to GPT on write).
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            if any(_is_raw_device_spelling(o) for o in operands):
                return "partition table edit"
        elif name == "cfdisk":
            # The curses partition editor: -r/--read-only forces a
            # read-only open; -V/--version and -h/--help only print
            # and exit. Every other form opens the editor, which
            # writes on Write — and with no device at all it defaults
            # to /dev/sda, so a bare `cfdisk` blocks too. There is no
            # print form to preserve: --print was removed in
            # util-linux 2.25 (printing moved to partx / lsblk).
            # Value options: -b/--sector-size takes a value;
            # -L/--color and --lock take optional arguments, so they
            # are NOT value-taking here — `-L /dev/sda` leaves the
            # device an operand, and consuming it would be a false
            # pass.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("b"),
                value_longs=frozenset(("sector-size",)))
            if shorts & {"r", "V", "h"} or longs & {
                    "read-only", "version", "help"}:
                continue
            if not operands:
                return "partition table edit"
            if any(_is_raw_device_spelling(o) for o in operands):
                return "partition table edit"
        elif name == "fixparts":
            # The GPT fdisk MBR repair tool: it takes no command-line
            # options at all, just the device, and its interactive
            # menu rewrites the partition table on `w` — it deletes
            # stray GPT data, changes primary/logical assignment, and
            # is liable to renumber partitions unasked.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            if any(_is_raw_device_spelling(o) for o in operands):
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
        elif name in ("vgcreate", "vgextend", "vgreduce"):
            # vgcreate claims the devices for a new volume group;
            # vgextend adds one to an existing group; vgreduce drops
            # one. Value options: -s physical extent size, -l max
            # logical volumes, -p max physical volumes,
            # --metadatatype, --vgmetadatacopies.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("slp"),
                value_longs=frozenset(
                    ("physicalextentsize", "maxlogicalvolumes",
                     "maxphysicalvolumes", "metadatatype",
                     "vgmetadatacopies")))
            if any(_is_raw_device_spelling(o) for o in operands):
                return "LVM metadata write"
        elif name == "vgremove":
            # Removes a whole volume group: its operand is a plain VG
            # name (``vgremove vg0``), not a device path — the shared
            # vg* branch above only fired when a member device was
            # named alongside it, so the canonical spelling passed
            # unchecked (verified against ``_is_dangerous`` before
            # this fix). The group IS the data, so any removal blocks,
            # the same way ``zpool destroy`` blocks a pool by name.
            # --test is the dry run (prints what would be done,
            # writes nothing); --select names groups to remove
            # without an operand, so it blocks too. Value options:
            # -S/--select takes the selection expression.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("S"),
                value_longs=frozenset(("select",)))
            if "test" in longs or "t" in shorts:
                continue
            if "select" in longs or "S" in shorts or operands:
                return "LVM metadata write"
        elif name == "lvremove":
            # Removes a logical volume: its operand is usually a plain
            # ``vg/lv`` name (``lvremove vg0/lv0``), not a /dev path —
            # the old check only knew /dev/mapper/vg-lv and
            # /dev/<vg>/<lv>, so the canonical spelling passed
            # unchecked (verified against ``_is_dangerous`` before
            # this fix). The volume IS the data, so any removal
            # blocks, the same way ``zpool destroy`` blocks a pool by
            # name. --test is the dry run (prints what would be done,
            # writes nothing); --select names volumes to remove
            # without an operand, so it blocks too. Value options:
            # -S/--select takes the selection expression.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("S"),
                value_longs=frozenset(("select",)))
            if "test" in longs or "t" in shorts:
                continue
            if "select" in longs or "S" in shorts or operands:
                return "LVM metadata write"
        elif name == "lvreduce":
            # lvreduce exists only to shrink a logical volume: every
            # block past the new end is discarded, so any invocation
            # naming a volume blocks — the same way lvremove blocks
            # a volume by name, and for the same reason: the volume
            # IS the data, and no device path ever appears (verified
            # against ``_is_dangerous`` before this fix). There is
            # no grow form to preserve: -L +1G is an error in
            # lvreduce. --test is the dry run; --select names
            # volumes to shrink without an operand. Value options:
            # -L/--size, -l/--extents, -S/--select.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("LlS"),
                value_longs=frozenset(
                    ("size", "extents", "select")))
            if "test" in longs or "t" in shorts:
                continue
            if "select" in longs or "S" in shorts or operands:
                return "LVM volume shrink"
        elif name == "lvresize":
            # lvresize sets a new size: -L/--size (e.g. 1G) or
            # -l/--extents (e.g. 100). A leading + grows the volume
            # and destroys nothing; an absolute size or a leading -
            # shrinks it, discarding every block past the new end —
            # the same data loss as lvreduce. Only the shrink
            # spellings block, so the grow forms (-L +1G, -l +100,
            # --size +100%FREE) stay allowed, as does the --test dry
            # run. With no size given the command errors in real
            # LVM, so it stays allowed, the same way a bare
            # lvremove does. Value options: -L/--size, -l/--extents,
            # -S/--select.
            operands, shorts, longs, values = _split_tool_args(
                args, value_shorts=frozenset("LlS"),
                value_longs=frozenset(
                    ("size", "extents", "select")))
            if "test" in longs or "t" in shorts:
                continue
            if not operands and "select" not in longs \
                    and "S" not in shorts:
                continue
            for flag, val in values:
                if flag in ("L", "l", "size", "extents") \
                        and not val.startswith("+"):
                    return "LVM volume shrink"
        elif name == "mdadm":
            # --create builds a new array (destroying the member
            # devices) and --zero-superblock wipes the RAID metadata
            # off a member, dropping it from its array; --detail /
            # --examine / --assemble / --stop only read or manage an
            # existing one. Value options: -l level, -n raid-devices,
            # -c chunk, -e metadata, -N name, -b bitmap, -p layout.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("lnceNbp"),
                value_longs=frozenset(
                    ("level", "raid-devices", "chunk", "name",
                     "metadata", "bitmap", "layout", "spare-devices",
                     "write-journal", "journal", "homehost", "nodes")))
            if not any(_is_raw_device_spelling(o) for o in operands):
                continue
            if "C" in shorts or "create" in longs:
                return "RAID array create"
            if "zero-superblock" in longs:
                return "RAID superblock wipe"
        elif name == "zpool":
            # `create` builds a pool on the devices and `destroy`
            # wipes a whole pool; status / list / get / iostat only
            # read. -n is a dry run (prints what would be done,
            # writes nothing). `destroy` takes a pool name, not a
            # device path — but the pool IS the data, so any destroy
            # except the -n dry run blocks, the same way `lvremove`
            # blocks a volume by name.
            operands, shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("oOmRt"),
                value_longs=frozenset())
            if not operands or operands[0] not in ("create",
                                                    "destroy"):
                continue
            if "n" in shorts:
                continue
            if operands[0] == "create":
                if any(_is_raw_device_spelling(o)
                       for o in operands[1:]):
                    return "ZFS pool create"
            else:
                return "ZFS pool destroy"
        elif name == "zfs":
            # `destroy` wipes a dataset (or a snapshot) and `rollback`
            # destroys every snapshot newer than the target; list /
            # get / snapshot / clone only read or create. -n is a dry
            # run for destroy (prints what would be done, writes
            # nothing); rollback has no dry run, so every rollback
            # blocks. The operand is a dataset name, not a device
            # path — but the dataset IS the data, so the name itself
            # is the target, the same way `lvremove` blocks a volume
            # by name.
            operands, shorts, _longs, _vals = _split_tool_args(args)
            if not operands or operands[0] not in ("destroy",
                                                    "rollback"):
                continue
            if operands[0] == "destroy" and "n" in shorts:
                continue
            return ("ZFS dataset destroy"
                    if operands[0] == "destroy"
                    else "ZFS snapshot rollback")
        elif name == "nvme":
            # Only the destructive subcommands are blocked: `format`
            # and `sanitize` erase the whole namespace, and — the
            # spellings this fix adds — `write` writes arbitrary data
            # over it, `write-zeroes` zeroes a range (the whole
            # namespace by default), `write-uncorrectable` flags
            # blocks unreadable, `dsm` with --ad deallocates (TRIMs)
            # the named ranges, and `delete-ns` removes the namespace
            # outright; all five passed unchecked while their format /
            # sanitize siblings were already blocked (verified against
            # ``_is_dangerous`` before this fix). Everything else
            # (list, smart-log, id-ctrl, id-ns, get-feature, and
            # `verify`, which only reads the namespace back) stays
            # allowed, as does `dsm` without --ad (write-zero /
            # integrity hints destroy nothing). Value options per
            # nvme --help: format takes -s/--ses, -l/--lbaf, -i/--pi,
            # -p/--pil, -m/--ms, -n/--namespace-id; sanitize takes
            # -a/--sanact and -p/--ovrpat; the write family takes
            # --slba/--nlb/--data-size/--data and kin. A value is
            # never a device in legitimate use, so a device spelling
            # in either position blocks — it is either an evasion
            # attempt (``-a /dev/nvme0n1``) or nonsense that would
            # fail in nvme anyway.
            operands, shorts, longs, values = _split_tool_args(
                args, value_shorts=frozenset("slipmnaczde"),
                value_longs=frozenset(
                    ("ses", "lbaf", "pi", "pil", "ms",
                     "namespace-id", "sanact", "ovrpat", "slba",
                     "nlb", "data-size", "data", "metadata-size",
                     "metadata", "apptag", "appmask", "reftag",
                     "control", "storage-tag", "cdw12", "cdw13",
                     "lr", "fua", "prinfo", "dtype", "dspec",
                     "dsmgmt", "timeout", "start-block",
                     "block-count")))
            if not operands:
                continue
            sub = operands[0]
            if sub == "dsm":
                # Only the deallocate form destroys: without --ad
                # (or -a) dsm sends non-destructive DSM hints. The
                # device is checked in the option values too: -a is a
                # flag in dsm (a value option in format), so the
                # shared split above may have consumed the device as
                # -a's "value" — and dsm's real values are numbers,
                # never device spellings.
                if "ad" not in longs and "a" not in shorts:
                    continue
                targets = list(operands[1:]) + [v for _f, v in values]
                if any(_is_raw_device_spelling(o) for o in targets):
                    return "NVMe dataset management deallocate"
                continue
            if sub not in ("format", "sanitize", "write",
                           "write-zeroes", "write-uncorrectable",
                           "delete-ns"):
                continue
            targets = list(operands[1:]) + [v for _f, v in values]
            if any(_is_raw_device_spelling(o) for o in targets):
                return {
                    "format": "NVMe namespace format",
                    "sanitize": "NVMe sanitize",
                    "write": "NVMe namespace write",
                    "write-zeroes": "NVMe write zeroes",
                    "write-uncorrectable": "NVMe write uncorrectable",
                    "delete-ns": "NVMe namespace delete",
                }[sub]
        elif name == "hdparm":
            # The firmware secure-erase forms destroy the disk:
            # --security-erase / --security-erase-enhanced wipe every
            # sector (the password is the option's value, consumed
            # above so it is never mistaken for the device) — and so
            # do the sector-write forms: --write-sector writes a
            # sector's worth of arbitrary data, --make-bad-sector
            # corrupts one, and --trim-sector-ranges discards the
            # listed ranges (the ranges are plain operands, never a
            # device spelling, so the device check below still only
            # fires on the disk). The read-only forms (-I, -g,
            # --read-sector, --security-freeze is a lock, not a wipe)
            # and password management (--security-set-pass,
            # --security-unlock, --security-disable) stay allowed:
            # they lock or unlock the drive but destroy no data.
            operands, _shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset(""),
                value_longs=frozenset(
                    ("security-erase", "security-erase-enhanced",
                     "security-set-pass", "security-unlock",
                     "security-disable", "user-master",
                     "write-sector", "make-bad-sector")))
            if not longs & {"security-erase",
                           "security-erase-enhanced",
                           "write-sector", "trim-sector-ranges",
                           "make-bad-sector"}:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                if longs & {"security-erase",
                           "security-erase-enhanced"}:
                    return "firmware secure erase"
                return "firmware sector write"
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
        elif name == "sg_format":
            # Only --format (-F) destroys the disk: it issues a SCSI
            # FORMAT UNIT command, which "will destroy all the data
            # held on the media" (sg_format(8)). A bare
            # ``sg_format /dev/sda`` only prints the block size and
            # block count and "will not modify the disk", and --resize
            # is documented as leaving every block's contents
            # unchanged and reversible — so both stay allowed, as do
            # the format parameters on their own (--size, --count,
            # --ffmt, --security only parametrize a format, and
            # --quick only skips the 15-second warning). --dry-run
            # parses the command line and bypasses the actual FORMAT
            # UNIT command, so it stays allowed even with --format.
            # Value options per sg_format --help (-C cmplst, -c
            # count, -t ffmt, -f fmtpinfo, -M mode, -P pfu, -q pie,
            # -x poll, -s size, --tape, --timeout).
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("CctfMPqxs"),
                value_longs=frozenset(
                    ("cmplst", "count", "ffmt", "fmtpinfo", "mode",
                     "pfu", "pie", "poll", "size", "tape",
                     "timeout")))
            if "dry-run" in longs:
                continue
            if "F" not in shorts and "format" not in longs:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "SCSI disk format"
        elif name in ("sg_dd", "sgm_dd", "sgp_dd", "dcfldd", "dc3dd"):
            # sg_dd (and its threaded twins sgm_dd / sgp_dd) is dd
            # under another name: ``sg_dd if=/dev/zero of=/dev/sda``
            # writes the device exactly like ``dd of=/dev/sda`` — but
            # the ``dd`` pattern needs a word boundary before ``dd``,
            # and the underscore in ``sg_dd`` defeats it, so every
            # such write passed unchecked (verified against
            # ``_is_dangerous`` before this fix). dcfldd (the
            # forensic dd with hashing) and dc3dd (its DoD fork) are
            # the same spelling under another name again: they take
            # the same ``if=`` / ``of=`` keywords, so ``dcfldd
            # if=/dev/zero of=/dev/sda`` writes the device exactly
            # like ``dd`` — but the ``dd`` in ``dcfldd`` / ``dc3dd``
            # defeats the same word boundary. Only the of= target
            # counts: reading a device to a file
            # (``sg_dd if=/dev/sda of=/tmp/x``) stays allowed, as
            # does writing to a harmless target such as /dev/null.
            # The args are key=value words, so the of= value is
            # picked out of the operands rather than split as a
            # flag; a value word such as ``bs=512`` never matches
            # the device spelling.
            operands, _shorts, _longs, _vals = _split_tool_args(args)
            for target in operands:
                if target.startswith("of=") and _is_raw_device_spelling(
                        target[3:]):
                    return "raw disk write"
        elif name == "ddrescue":
            # ddrescue copies its infile to a plain positional
            # outfile: ``ddrescue /dev/sda /dev/sdb`` overwrites the
            # device exactly like ``dd of=/dev/sdb`` — but the
            # outfile is a positional operand, so no ``dd`` / copy /
            # wipe pattern ever saw it (verified against
            # ``_is_dangerous`` before this fix). Only the outfile
            # (the second operand) counts: the infile may be a
            # device — reading it is the point — and an outfile
            # that is a regular file stays allowed, as does a
            # mapfile that names a device (it only records block
            # status). Value options per the GNU ddrescue manual
            # (-a min read rate, -b sector size, -c cluster size,
            # -e max bad areas, -E max error rate, -i input
            # position, -K skip size, -m domain mapfile, -o output
            # position, -r retry passes, -s size, -T timeout, -X
            # max read errors).
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("abceEiKmorsTX"),
                value_longs=frozenset(
                    ("min-read-rate", "sector-size", "cluster-size",
                     "max-bad-areas", "max-errors", "max-error-rate",
                     "input-position", "skip-size", "domain-mapfile",
                     "output-position", "retry-passes", "size",
                     "timeout", "max-read-errors")))
            if len(operands) >= 2 and _is_raw_device_spelling(
                    operands[1]):
                return "raw disk write"
        elif name == "sg_write_same":
            # WRITE SAME writes one block NUM times from LBA — with
            # --num=0 it writes to the end of the device, so a single
            # invocation can clear the whole disk (the sg_write_same(8)
            # man page warns exactly that) — but no check knew the
            # tool at all (verified against ``_is_dangerous`` before
            # this fix). The utility itself refuses to run without one
            # of --in / --lba / --num, so a bare ``sg_write_same
            # /dev/sda`` (which errors out) stays allowed, and a
            # device named only as the --in data source stays allowed
            # too — reading it is not a write. Value options per
            # sg_write_same(8) (-g grpnum, -i in, -l lba, -n num, -t
            # timeout, -w wrprotect, -x xferlen).
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("gilntwx"),
                value_longs=frozenset(
                    ("grpnum", "in", "lba", "num", "timeout",
                     "wrprotect", "xferlen")))
            if not (shorts & {"i", "l", "n"}
                    or longs & {"in", "lba", "num"}):
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "SCSI write same"
        elif name == "sg_unmap":
            # UNMAP discards the named blocks — the SCSI equivalent
            # of blkdiscard, which is already blocked — and --all
            # unmaps from its start LBA to the end of the device, so
            # ``sg_unmap --all=0,1k /dev/sda`` discards the whole
            # disk; the man page gives it a 15-second cooling-off
            # period "since a lot of data can be lost with this
            # utility" — but no check knew the tool at all (verified
            # against ``_is_dangerous`` before this fix). -d /
            # --dry-run does the preparation and exits before any
            # UNMAP is sent, so it stays allowed, the same way the
            # --test dry runs of the LVM tools do. Value options per
            # sg_unmap(8) (-A all, -g grpnum, -I in, -l lba, -n num,
            # -t timeout). The --in file only names LBA/num pairs,
            # so a device spelling there is a filename, never a
            # write target — it is consumed as a value, not an
            # operand.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("AgIlnt"),
                value_longs=frozenset(
                    ("all", "grpnum", "in", "lba", "num", "timeout")))
            if "d" in shorts or longs & {"dry-run", "dry_run"}:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "SCSI unmap"
        elif name == "sg_write_buffer":
            # WRITE BUFFER sends data to the device — and modes 4-7
            # download microcode, i.e. flash the device firmware —
            # but no check knew the tool at all (verified against
            # ``_is_dangerous`` before this fix). There is no
            # read-only form: every non-dry-run invocation sends a
            # WRITE BUFFER command to the named device. -d /
            # --dry-run steps over the command and stays allowed.
            # Value options per sg_write_buffer(8) (-b bpw, -i id,
            # -I in, -l length, -m mode, -o offset, -s skip, -S
            # specific, -t timeout).
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("biIlmost"),
                value_longs=frozenset(
                    ("bpw", "id", "in", "length", "mode", "offset",
                     "skip", "specific", "timeout")))
            if "d" in shorts or longs & {"dry-run", "dry_run"}:
                continue
            if any(_is_raw_device_spelling(o) for o in operands):
                return "SCSI write buffer"
        elif name == "sg_write_long":
            # WRITE LONG overwrites one block (plus ECC) at LBA —
            # LBA 0 by default — with 0xff bytes unless --in gives
            # the data, and --wr_uncor flags the block unrecoverable
            # without transferring any data; either way the block is
            # gone, and the man page's own workflow is to save the
            # block with sg_read_long first because the write
            # destroys it — but no check knew the tool at all
            # (verified against ``_is_dangerous`` before this fix).
            # No dry run and no safety interlock exist, so a bare
            # ``sg_write_long /dev/sda`` blocks too. Value options
            # per sg_write_long(8) (-i in, -l lba, -x xfer_len).
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("ilx"),
                value_longs=frozenset(("in", "lba", "xfer_len",
                                       "xfer-len")))
            if any(_is_raw_device_spelling(o) for o in operands):
                return "SCSI write long"
        elif name == "dd_rescue":
            # dd_rescue (Kurt Garloff's, with the underscore) copies
            # its infile to a plain positional outfile exactly like
            # the ddrescue covered above — ``dd_rescue /dev/zero
            # /dev/sda`` overwrites the device — but the ``dd`` in
            # its name defeats the ``dd`` word boundary the same way
            # ``dcfldd`` / ``dc3dd`` did, and its name never matched
            # the ``ddrescue`` check either (verified against
            # ``_is_dangerous`` before this fix). Only the outfile
            # counts: the infile may be a device — reading it is the
            # point — and an outfile that is a regular file stays
            # allowed. In shred mode (-2/-3/-4/-z/-Z and the
            # --shred2/--shred3/--shred4/--random/--frandom longs)
            # there is no infile: the single operand IS the outfile
            # the random data is written to, so that operand counts
            # instead. -Y/--outfile/--of names secondary output
            # files the same data is written to, and -l/--logfile
            # and -o/--bbfile are file outputs too, so a device in
            # any of them blocks, the same way rsync's --log-file
            # and wget's -o do. Value options per dd_rescue(1) (-b
            # softbs, -B hardbs, -e maxerr, -l logfile, -o bbfile,
            # -y syncfreq, -s ipos, -S opos, -m maxxfer, -C
            # ratecontrol, -@ sparse_nonslow, -2/-3/-4/-z/-Z seed,
            # -c color, -Y outfile).
            operands, shorts, longs, values = _split_tool_args(
                args, value_shorts=frozenset("bBeCloSsmYy@234zZc"),
                value_longs=frozenset(
                    ("softbs", "bs", "hardbs", "block-size", "maxerr",
                     "logfile", "bbfile", "syncfreq", "ipos",
                     "input-position", "opos", "output-position",
                     "maxxfer", "max-size", "ratecontrol",
                     "sparse_nonslow", "shred2", "shred3", "shred4",
                     "random", "frandom", "outfile", "of", "color")))
            if any(_is_raw_device_spelling(v)
                   for flag, v in values
                   if flag in ("Y", "outfile", "of", "l", "logfile",
                               "o", "bbfile")):
                return "raw disk write"
            if shorts & {"2", "3", "4", "z", "Z"} or longs & {
                    "shred2", "shred3", "shred4", "random", "frandom"}:
                if operands and _is_raw_device_spelling(operands[-1]):
                    return "raw disk write"
            elif len(operands) >= 2 and _is_raw_device_spelling(
                    operands[1]):
                return "raw disk write"
        elif name == "sg_sanitize":
            # SANITIZE is the SCSI erase command family: --block
            # block-erases, --crypto crypto-erases, --overwrite
            # overwrites, --fail flags every block failed — each
            # destroys the media exactly like the already-blocked
            # ``sg_format --format``, but no check knew the tool
            # (verified against ``_is_dangerous`` before this fix).
            # There is no read-only run mode: only --help / --version
            # print and exit. The device may be a /dev/sgN node, which
            # the raw-device spellings do not enumerate, so those are
            # matched here as well.
            operands, _shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("ot"),
                value_longs=frozenset(
                    ("overwrite", "test", "timeout")))
            if longs & {"help", "version"}:
                continue
            if any(_is_raw_device_spelling(o)
                   or _SG_DEVICE.match(_normalize_device_spelling(o))
                   for o in operands):
                return "SCSI sanitize"
        elif name == "fio":
            # fio benchmarks a target by writing patterns over it:
            # with a block device as --filename and a write-class
            # --rw mode (write / randwrite / rw / trim / …) it
            # overwrites the disk exactly like ``dd`` — but no check
            # knew the tool (verified against ``_is_dangerous``
            # before this fix). Read modes on a device stay allowed
            # (benchmarking reads is the point), as do write modes on
            # a regular file. --filename takes a colon-separated list;
            # every entry is priced. The mode may come from --rw or
            # its long alias --readwrite, attached or separate.
            filename_vals: list[str] = []
            mode_vals: list[str] = []
            j = 0
            while j < len(args):
                arg = args[j]
                key, eq, val = arg.partition("=")
                if key in ("--filename", "--rw", "--readwrite"):
                    if not eq and j + 1 < len(args):
                        val = args[j + 1]
                        j += 1
                    (filename_vals if key == "--filename"
                     else mode_vals).append(val)
                j += 1
            write_modes = {"write", "randwrite", "rw", "randrw",
                           "trim", "randtrim", "trimwrite",
                           "randtrimwrite"}
            if not any(m.split(":")[0].strip() in write_modes
                       for m in mode_vals):
                continue
            if any(_is_device_target(entry)
                   for val in filename_vals
                   for entry in val.split(":") if entry):
                return "raw disk write"
        elif name == "fallocate":
            # --zero-range / --punch-hole / --collapse-range (and
            # their -z / -p / -c shorts) discard the named range in
            # place: on a block device they zero / deallocate the
            # disk's blocks exactly like the already-blocked
            # ``blkdiscard`` — but no check knew the spelling
            # (verified against ``_is_dangerous`` before this fix).
            # The same hole stayed open for a plain allocation: ``-l``
            # / ``--length`` reserves space on the target, which on a
            # raw device is a write to the disk like ``dd`` — also
            # verified against ``_is_dangerous`` before this fix. A
            # plain fallocate only reserves space, and the discard and
            # allocation modes on a regular file are ordinary file
            # work, so both stay allowed. Value options per fallocate
            # --help (-l length, -o offset). A symlink landing on a
            # device counts too, like the scrub branch below.
            operands, shorts, longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("lo"),
                value_longs=frozenset(("length", "offset")))
            discard = shorts & {"z", "p", "c"} or longs & {
                "zero-range", "punch-hole", "collapse-range"}
            allocate = "l" in shorts or "length" in longs
            if not (discard or allocate):
                continue
            if any(_is_device_target(o) for o in operands):
                return ("raw device discard" if discard
                        else "raw disk write")
        elif name == "truncate":
            # -s/--size sets the target's size: on a raw device that
            # is a write to the disk exactly like ``dd of=`` — but no
            # check knew the tool (verified against ``_is_dangerous``
            # before this fix). Truncating a regular file is ordinary
            # file work and stays allowed. -r/--reference only names
            # the file whose size is copied, so its value is consumed
            # as a value and never mistaken for a target.
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("sr"),
                value_longs=frozenset(("size", "reference")))
            if any(_is_device_target(o) for o in operands):
                return "raw disk write"
        elif name == "scrub":
            # scrub writes pattern passes over its target file: on a
            # raw device that is a full-disk overwrite, the same write
            # ``shred`` is blocked for — but no check knew the tool
            # (verified against ``_is_dangerous`` before this fix).
            # Scrubbing a regular file is the tool's ordinary use and
            # stays allowed. Value options per scrub --help (-p
            # pattern file, -D directory prefix, -b block size).
            operands, _shorts, _longs, _vals = _split_tool_args(
                args, value_shorts=frozenset("pDb"),
                value_longs=frozenset(
                    ("pattern-file", "dir-prefix", "blocksize")))
            if any(_is_device_target(o) for o in operands):
                return "raw disk wipe"
    return None


# A SCSI generic device node: the sg_* tools take /dev/sgN as well as
# the named disk, and the raw-device spellings do not enumerate it.
_SG_DEVICE = re.compile(r"^/dev/sg[0-9]+$")


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
# admits the hyphen in ``dm-0``. The MTD flash devices (``mtd*`` /
# ``mtdblock*`` / ``ubi*``), s390 DASD disks (``dasd*``) and Xen
# virtual disks (``xvd*``) are covered the same way — a redirect
# onto one overwrites the flash / disk exactly like ``> /dev/sda``.
# The ``mtd`` branch consumes the digit run and asserts the next
# character is not a letter or digit, so the read-only ``mtdNro``
# nodes stay allowed the way ``mem`` / ``kmem`` / ``port`` keep
# ``/dev/memory`` allowed. Kept at module level so the
# brace-spelling re-check in ``_is_dangerous`` can reuse the same
# patterns instead of duplicating them; the spelling rules are
# explained where they are used, above the ``dd`` pattern.
_DEVICE_REDIRECT_PATTERNS = [
    (r">\|?\s*" + _Q + r"/+" + _Q + r"dev/" + _Q + r"(?:\./|/)*" + _Q
     + r"(s" + _Q + r"d|h" + _Q + r"d|n" + _Q + r"v" + _Q + r"m" + _Q
     + r"e|v" + _Q + r"d|d" + _Q + r"m|l" + _Q + r"o|m" + _Q + r"m"
     + _Q + r"c" + _Q + r"b" + _Q + r"l" + _Q + r"k|m" + _Q + r"d"
     + r"|r" + _Q + r"a|r" + _Q + r"b|n" + _Q + r"b"
     + r"|x" + _Q + r"v" + _Q + r"d|d" + _Q + r"a" + _Q + r"s" + _Q + r"d"
     + r"|u" + _Q + r"b" + _Q + r"i"
     + r"|m" + _Q + r"t" + _Q + r"d" + _Q + r"[0-9]+(?![a-z0-9])"
     + r"|m" + _Q + r"t" + _Q + r"d" + _Q + r"b" + _Q + r"l" + _Q
     + r"o" + _Q + r"c" + _Q + r"k)(?:" + _Q
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
     + r"|r" + _Q + r"a|r" + _Q + r"b|n" + _Q + r"b"
     + r"|x" + _Q + r"v" + _Q + r"d|d" + _Q + r"a" + _Q + r"s" + _Q + r"d"
     + r"|u" + _Q + r"b" + _Q + r"i"
     + r"|m" + _Q + r"t" + _Q + r"d" + _Q + r"[0-9]+(?![a-z0-9])"
     + r"|m" + _Q + r"t" + _Q + r"d" + _Q + r"b" + _Q + r"l" + _Q
     + r"o" + _Q + r"c" + _Q + r"k)(?:" + _Q
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


def _dd_reason(tokens: list[str]) -> str | None:
    """Token-stage ``dd`` check: the ``of=`` target, priced like a path.

    The raw ``dd`` pattern is the historical check, but it prices the
    *spelling*: a target produced by an assignment (``of=$D`` with
    ``D=/dev/sda`` priced by ``_resolve_simple_assignments``), hidden
    behind a symlink (``of=/tmp/disklink``), or aimed at the SysRq
    trigger all missed it, because a pattern over the raw string
    cannot resolve any of those (verified against ``_is_dangerous``
    before this fix). Here the target is a token, so the device,
    realpath and SysRq prices all apply. Only ``of=`` counts: reading
    a device (``if=/dev/sda``) to a file stays allowed.
    """
    for i, tok in enumerate(tokens):
        if _basename(tok) != "dd":
            continue
        args = _args_until_separator(tokens, i + 1)
        for arg in args:
            if not arg.startswith("of="):
                continue
            target = arg[3:].strip("\"'")
            if _is_device_target(target):
                return "raw disk write"
            if _normalize_path_spelling(target) == "/proc/sysrq-trigger":
                return "power control"
    return None


def _pipe_reason(tokens: list[str]) -> str | None:
    """A pipe whose sink executes what it receives.

    The raw pattern only knew the download one-liner (``curl`` /
    ``wget`` piped to a shell). The producer is irrelevant to the
    sink: ``echo 'rm -rf /' | bash``, ``cat script | sh`` and a base64
    decode piped to ``bash`` execute attacker-chosen text exactly
    like the download does, and piping into ``at`` or ``crontab -``
    is worse — the payload runs later, unattended (all verified
    against ``_is_dangerous`` before this fix). Any pipe into a shell
    word, ``at``, or a crontab install is therefore refused; a pipe
    into ``crontab -l`` only lists and stays allowed, as do pipes
    into ordinary filters (``grep``, ``sha256sum``, ``tee`` …).
    """
    for i, tok in enumerate(tokens):
        if tok != "|":
            continue
        j = i + 1
        if j < len(tokens) and _basename(tokens[j]) == "sudo":
            j += 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 1
        if j >= len(tokens):
            continue
        sink = _basename(tokens[j])
        sink_args = _args_until_separator(tokens, j + 1)
        if sink in _SHELLS:
            return "piped to a shell"
        if sink == "at":
            return "piped to a job scheduler"
        if sink == "crontab":
            if all(a in ("-l", "--list") for a in sink_args):
                continue
            return "piped to crontab"
    return None


def _power_reason(tokens: list[str]) -> str | None:
    """Availability attacks the power-word pattern never named.

    ``shutdown`` / ``reboot`` / ``halt`` / ``poweroff`` are raw
    patterns, but their equivalents passed unchecked (verified
    against ``_is_dangerous`` before this fix): ``init 6`` /
    ``telinit 0`` change runlevel directly; ``kill … 1`` kills the
    init process (fatal inside a container / namespace, where PID 1
    is the container); and a write to ``/proc/sysrq-trigger`` —
    spelled as a ``tee`` / ``cp`` / ``mv`` / ``install`` destination
    here, as a redirect and as ``dd of=`` elsewhere — forces an
    immediate unsynced reboot (``echo b``), crash or poweroff. The
    SysRq *file* in another directory (``/tmp/sysrq-trigger``) is a
    plain file and stays allowed, as do ``init --help``, ``kill`` of
    an ordinary pid, and ``kill %1`` (a jobspec, not PID 1).
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        if name in ("init", "telinit"):
            if any(a in ("0", "6") for a in args
                   if not a.startswith("-")):
                return "power control"
        elif name == "kill":
            operands: list[str] = []
            j = 0
            while j < len(args):
                arg = args[j]
                if arg in ("-s", "--signal"):
                    j += 2
                    continue
                if arg.startswith("-") and arg != "-":
                    j += 1
                    continue
                operands.append(arg)
                j += 1
            if "1" in operands:
                return "kill of PID 1 (init)"
        elif name in ("tee", "cp", "mv", "install"):
            operands = [a for a in args if not a.startswith("-")]
            targets = operands if name == "tee" else operands[-1:]
            if any(_normalize_path_spelling(a.strip("\"'"))
                   == "/proc/sysrq-trigger" for a in targets):
                return "power control"
    return None


_XARGS_VALUE_SHORTS = frozenset("EILnPsad")
_XARGS_VALUE_LONGS = frozenset(
    ("arg-file", "delimiter", "eof", "max-args", "max-chars",
     "max-lines", "max-procs", "replace", "process-slot-var"))


def _xargs_reason(tokens: list[str]) -> str | None:
    """``xargs`` feeding a destructive verb targets the scan cannot see.

    With ``xargs`` the destructive target arrives on stdin, so the
    tokens after the verb name no guarded path at all: ``echo / |
    xargs rm -rf`` deletes ``/`` and ``printf '/dev/sda' | xargs
    -I{} dd of={}`` writes the device, yet both passed every check
    (verified against ``_is_dangerous`` before this fix). A recursive
    ``rm`` under xargs is therefore refused outright — its targets
    are by construction unpriceable here — while a plain ``xargs rm``
    (deleting listed files, the ordinary use) stays allowed. ``dd``
    under xargs is priced on its literal ``of=``: a raw device, a
    symlink to one, or the ``-I`` replacement string standing in for
    stdin-supplied names all refuse.
    """
    for i, tok in enumerate(tokens):
        if _basename(tok) != "xargs":
            continue
        args = _args_until_separator(tokens, i + 1)
        # The command word is the first operand; everything after it
        # is the command's own argument list, flags included — so
        # scan positionally instead of splitting flags away.
        cmd_at: int | None = None
        j = 0
        while j < len(args):
            arg = args[j]
            if arg == "--":
                cmd_at = j + 1 if j + 1 < len(args) else None
                break
            if arg.startswith("--") and len(arg) > 2:
                name, eq, _val = arg[2:].partition("=")
                j += 1 if eq or name not in _XARGS_VALUE_LONGS else 2
                continue
            if arg.startswith("-") and arg != "-":
                consumed_next = False
                k = 1
                while k < len(arg):
                    if arg[k] in _XARGS_VALUE_SHORTS:
                        consumed_next = k + 1 >= len(arg)
                        break
                    k += 1
                j += 2 if consumed_next else 1
                continue
            cmd_at = j
            break
        if cmd_at is None:
            continue  # no command word: xargs runs echo
        cmd = _basename(args[cmd_at])
        cmd_args = args[cmd_at + 1:]
        if cmd == "rm":
            recursive = any(
                a == "--recursive"
                or (a.startswith("-") and not a.startswith("--")
                    and ("r" in a[1:] or "R" in a[1:]))
                for a in cmd_args)
            if recursive:
                return "recursive delete via xargs"
        elif cmd == "dd":
            repl: str | None = None
            for j, arg in enumerate(args):
                if arg in ("-I", "--replace"):
                    repl = args[j + 1] if j + 1 < len(args) else "{}"
                elif arg.startswith("--replace="):
                    repl = arg.partition("=")[2]
                elif arg.startswith("-I") and len(arg) > 2:
                    repl = arg[2:]
                elif arg == "-i":
                    repl = "{}"  # -i implies -I{} in GNU xargs
            for arg in cmd_args:
                if not arg.startswith("of="):
                    continue
                target = arg[3:].strip("\"'")
                if _is_device_target(target):
                    return "raw disk write"
                if repl and repl in target:
                    return "raw disk write"
    return None


# Inline code handed to an interpreter is code the shell-level checks
# cannot model: no ``rm`` token, no device token in a checked
# position, no shell operator — yet the interpreter performs the
# exact operations the blocklist exists to stop (recursive delete of
# ``/``, raw writes to ``/dev/sda``, a ``system()`` back into a
# shell). The payload is therefore scanned for the destructive
# vocabulary of the interpreter world: process-spawning and
# file-destroying APIs, raw-device names, and shell spellings inside
# its string literals (each literal is rescanned as a command).
# Benign one-liners (``python3 -c 'print(1)'``) name none of these
# and stay allowed (all the blocked spellings verified against
# ``_is_dangerous`` before this fix).
_CODE_PAYLOAD_PATTERNS = [
    (r"rmtree|os\.remove|os\.unlink|os\.rmdir|os\.system|os\.popen"
     r"|os\.exec|subprocess|shutil|child_process|execSync|spawnSync"
     r"|os\.execute|shell_exec|passthru\s*\(|system\s*\(|system\s+[\"']"
     r"|popen\s*\(",
     "destructive call in inline code"),
    (r"/dev/(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+"
     r"|mmcblk[0-9]+|dm-[0-9]+|loop[0-9]+|md[0-9]+|ram[0-9]+"
     r"|nbd[0-9]+|rbd[0-9]+|mtd[0-9]+|mtdblock[0-9]+|dasd[a-z]+"
     r"|xvd[a-z]+|mem\b|kmem\b|port\b)",
     "raw device access in inline code"),
    (r"\bmkfs\b|\bdd\b[^;]*\bof\s*=|\brm\s+-[a-zA-Z]*[rR]",
     "destructive command in inline code"),
]

_STRING_LITERAL = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def _code_payload_reason(payload: str, depth: int) -> str | None:
    """Scan an interpreter's inline code argument for destruction."""
    for pattern, reason in _CODE_PAYLOAD_PATTERNS:
        if re.search(pattern, payload):
            return reason
    if depth < 3:
        reason = _is_dangerous(payload, depth + 1)
        if reason:
            return reason
        for m in _STRING_LITERAL.finditer(payload):
            literal = m.group(1) if m.group(1) is not None else m.group(2)
            if literal and literal != payload:
                reason = _is_dangerous(literal, depth + 1)
                if reason:
                    return reason
    return None


_PIPE_STOPS = {"|", "&&", "||", "&", "`"}


def _args_until_pipe(tokens: list[str], start: int) -> list[str]:
    """Tokens from *start* up to the next pipe / chain operator.

    Unlike ``_args_until_separator`` this does not stop at ``;``:
    inside an interpreter's code argument a semicolon is a statement
    separator, not a command boundary, and the code must be priced
    whole (see ``_interpreter_reason``).
    """
    out: list[str] = []
    for tok in tokens[start:]:
        if tok in _PIPE_STOPS:
            break
        out.append(tok)
    return out


def _inline_code_payload(args: list[str], flag_chars: frozenset,
                         flag_longs: frozenset,
                         value_shorts: frozenset) -> str | None:
    """The code argument of a ``-c``/``-e``-style flag, or None.

    Mirrors the interpreters' own parsing: the flag may end a short
    cluster (``-ec '…'``), carry its code attached (``-c'…'``), or
    take the next word; a first non-option word is a script file,
    not inline code, and ends the search. The payload is rejoined
    from the operator-split fragments (``_split_shell_operators``
    breaks ``os.system("…")`` into pieces at the parentheses and the
    code's own semicolons), so the scan sees the whole program text;
    words after the program (the program's own argv) are scanned
    along with it, which can only refuse text that is itself
    destructive.
    """
    j = 0
    while j < len(args):
        arg = args[j]
        if arg == "--":
            return None
        if arg.startswith("--"):
            name, eq, val = arg[2:].partition("=")
            if name in flag_longs:
                if eq:
                    return " ".join([val] + args[j + 1:])
                return " ".join(args[j + 1:]) or None
            j += 1
            continue
        if arg.startswith("-") and arg != "-":
            k = 1
            consumed_next = False
            while k < len(arg):
                ch = arg[k]
                if ch in flag_chars:
                    rest = arg[k + 1:]
                    if rest:
                        return " ".join([rest] + args[j + 1:])
                    return " ".join(args[j + 1:]) or None
                if ch in value_shorts:
                    consumed_next = k + 1 >= len(arg)
                    break
                k += 1
            j += 2 if consumed_next else 1
            continue
        return None
    return None


# basename -> (code-flag short chars, code-flag long names,
#              value-taking short options before the code flag)
_INTERPRETERS = (
    (re.compile(r"^python[0-9]*(\.[0-9]+)?$"),
     frozenset("c"), frozenset(), frozenset("WX")),
    (re.compile(r"^perl[0-9.]*$"),
     frozenset("eE"), frozenset(), frozenset("IMm")),
    (re.compile(r"^ruby[0-9.]*$"),
     frozenset("e"), frozenset(), frozenset("Ir")),
    (re.compile(r"^node(js)?$"),
     frozenset("ep"), frozenset(("eval", "print")), frozenset("r")),
    (re.compile(r"^php[0-9.]*$"),
     frozenset("r"), frozenset(), frozenset("dc")),
    (re.compile(r"^lua(jit)?[0-9.]*$"),
     frozenset("e"), frozenset(), frozenset("l")),
)


def _interpreter_reason(tokens: list[str], depth: int) -> str | None:
    """Interpreter one-liners and ``ssh`` remote commands.

    ``python3 -c``, ``perl -e``, ``ruby -e``, ``node -e``, ``php
    -r``, ``lua -e``: the code argument is scanned by
    ``_code_payload_reason``. ``awk`` programs are refused when they
    shell out via ``system(`` (the program is the first operand;
    ``-f`` file programs are out of scope, like other script files
    the scan does not read). ``ssh host 'cmd'`` runs ``cmd`` on
    another machine with the same blast radius, so the remote
    command string is rescanned as a command; an interactive
    ``ssh host`` (no command) names nothing to scan.
    """
    for i, tok in enumerate(tokens):
        name = _basename(tok)
        args = _args_until_pipe(tokens, i + 1)
        if name in ("awk", "gawk", "mawk", "nawk"):
            # The program arrives fragmented by operator splitting
            # (parentheses and braces are split points), so price the
            # rejoined text: an awk program that shells out through
            # system() runs an arbitrary command.
            if re.search(r"system\s*\(|system\s+[\"']", " ".join(args)):
                return "destructive call in inline code"
            continue
        if name == "ssh":
            operands, _s, _l, _v = _split_tool_args(
                args, value_shorts=frozenset("bcDFiJlmOopRSWw"),
                value_longs=frozenset(
                    ("bind-address", "ciphers", "identity",
                     "jump", "login-name", "option", "port",
                     "proxy-command", "request-tty", "forward",
                     "local-forward", "remote-forward",
                     "dynamic-forward", "config", "macs",
                     "hostname", "user")))
            if len(operands) >= 2 and depth < 3:
                reason = _is_dangerous(" ".join(operands[1:]),
                                       depth + 1)
                if reason:
                    return reason
            continue
        for pattern, flag_chars, flag_longs, value_shorts in \
                _INTERPRETERS:
            if not pattern.match(name):
                continue
            payload = _inline_code_payload(
                args, flag_chars, flag_longs, value_shorts)
            if payload:
                reason = _code_payload_reason(payload, depth)
                if reason:
                    return reason
            break
    return None


_SCRIPT_SCAN_MAX = 1_000_000


def _shell_invocation_kind(tokens: list[str], start: int):
    """Classify the shell invocation at *start* (just past the shell).

    Returns ``("c", payload)`` for a ``-c`` invocation (the nested
    rescan owns it), ``("stdin", None)`` for ``-s``, ``("file",
    path)`` when the first operand is a script file, and ``(None,
    None)`` for an interactive invocation. The option walk mirrors
    ``_shell_c_payload`` (attached arguments, clusters, value-taking
    options), additionally noting the ``-s`` flag.
    """
    j = start
    stdin = False
    while j < len(tokens):
        tok = tokens[j]
        if tok in _SEPARATORS:
            break
        if tok == "--":
            if j + 1 < len(tokens) and tokens[j + 1] not in _SEPARATORS:
                return ("stdin", None) if stdin else ("file",
                                                      tokens[j + 1])
            return ("stdin", None) if stdin else (None, None)
        if tok.startswith("--"):
            if tok == "--stdin":
                stdin = True
                j += 1
                continue
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
                    return ("c", None)
                if ch == "s":
                    stdin = True
                if ch == "o":
                    consumed_value = k + 1 >= len(tok)
                    break
                k += 1
            j += 2 if consumed_value else 1
            continue
        return ("stdin", None) if stdin else ("file", tok)
    return ("stdin", None) if stdin else (None, None)


def _script_file_reason(tokens: list[str], depth: int) -> str | None:
    """A script file's contents are executed — so scan the contents.

    ``bash /tmp/x.sh`` ran whatever the file held without any check:
    the scan priced the command line, never the script (verified
    against ``_is_dangerous`` before this fix). The agent can create
    the file with its own write tool and then run it, so at exec time
    the file exists and its contents are scanned here, recursively.
    A missing file stays allowed (the shell errors, nothing runs),
    and a benign script (``echo hi``, ``rm -rf build/``) scans clean.
    ``source`` / ``.`` files get the same treatment. ``shell -s``
    reads the script from stdin: with a file redirect the contents
    are unknowable here and the invocation is refused; a heredoc
    body is part of the command string, so the ordinary token scan
    already sees it, and a pipe into a shell is ``_pipe_reason``'s.
    Only command positions are considered (start of a command, after
    a separator, or after ``sudo``), so ``echo bash`` and
    ``cp x .`` are never mistaken for invocations.
    """
    if depth >= 3:
        return None
    for i, tok in enumerate(tokens):
        if not (i == 0 or tokens[i - 1] in _SEPARATORS
                or _basename(tokens[i - 1]) == "sudo"):
            continue
        name = _basename(tok)
        args = _args_until_separator(tokens, i + 1)
        target: str | None = None
        if name in _SHELLS:
            kind, val = _shell_invocation_kind(tokens, i + 1)
            if kind == "stdin":
                for arg in args:
                    if arg == "<" or re.match(r"^\d*<(?![<])", arg) \
                            or (arg.startswith("<")
                                and not arg.startswith("<<")):
                        return "shell script read from a redirected file"
                continue
            if kind != "file":
                continue
            target = val
        elif tok in ("source", "."):
            operands = [a for a in args if not a.startswith("-")]
            if not operands:
                continue
            target = operands[0]
        else:
            continue
        if target is None:
            continue
        path = target.strip("\"'")
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getsize(path) > _SCRIPT_SCAN_MAX:
                return "script file too large to scan"
            text = Path(path).read_text(encoding="utf-8",
                                        errors="replace")
        except OSError:
            continue
        reason = _is_dangerous(text, depth + 1)
        if reason:
            return reason
    return None


# A command shlex cannot tokenize at all is not thereby safe: the
# raw patterns above have already run, and this last-resort scan
# covers the token-stage families with loose spellings, so an
# unparseable command carrying a destructive construct is refused
# rather than waved through (the fail-open hole of AK-01).
_UNPARSEABLE_MARKERS = [
    (r"\brm\b[^|;&\n]*-[a-zA-Z]*[rR][a-zA-Z]*\b[^|;&\n]*"
     r"(?:/+|~|\$HOME|\$\{HOME|/home)",
     "recursive delete of / or home directory"),
    (r"\bfind\b[^|;&\n]*\s/[^|;&\n]*-(?:delete|exec)",
     "find deleting from / or home directory"),
    (r"\b(?:shred|wipefs|blkdiscard)\b[^|;&\n]*/dev/",
     "raw disk wipe"),
    (r"\b(?:cp|mv|tee|install)\b[^|;&\n]*/dev/"
     r"(?:sd|hd|vd|nvme|mmcblk|mtd|dm-|loop|md[0-9]|nbd|rbd)",
     "raw disk write"),
    (r"\b(?:mke2fs|mkswap|fdisk|sfdisk|parted|sgdisk|gdisk|cgdisk"
     r"|cfdisk|fixparts|sg_format|sg_sanitize)\b[^|;&\n]*/dev/",
     "filesystem format"),
    (r"\bnvme\s+(?:format|sanitize|write-zeroes|write)\b"
     r"[^|;&\n]*/dev/",
     "NVMe namespace format"),
    (r"\bchmod\b[^|;&\n]*(?:[0-7]*777|a\+rwx|a=rwx)\s*/+(?:\s|$|[|;&])",
     "permission nuke"),
]


def _unparseable_reason(command: str) -> str | None:
    """Last-resort verdict for a command no tokenization could parse."""
    for pattern, reason in _UNPARSEABLE_MARKERS:
        if re.search(pattern, command):
            return reason
    return None


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
    device (and ``curl``/``wget``/``rsync``/``scp`` output targets),
    archive tools writing their output archive onto one (``tar -cf``,
    ``zip``, ``7z a``, ``cpio -o -F``, ``mksquashfs``,
    ``genisoimage -o``), and disk-image tools writing their output
    image onto one (``qemu-img convert`` / ``create``,
    ``VBoxManage convertfromraw`` / ``clonemedium``,
    ``virt-sparsify`` / ``virt-resize``),
    ``shred``/``wipefs``/``blkdiscard`` aimed at one, and
    format / partition / volume tools aimed at one (``mke2fs``,
    ``mkswap``, ``fdisk``, ``parted``, ``sgdisk``, ``sfdisk``,
    ``gdisk`` / ``cgdisk`` / ``cfdisk`` / ``fixparts``,
    ``cryptsetup luksFormat`` / ``erase``, ``pvcreate``/``vgcreate``
    and kin, ``nvme format`` / ``sanitize``, ``hdparm
    --security-erase`` / sector writes, ``badblocks -w``,
    ``mdadm --zero-superblock``, ``zpool destroy``, ``zfs destroy``
    / ``rollback`` — each keeping its read-only form) — including
    ``lvremove`` / ``vgremove`` by plain name (``vg0/lv0``, ``vg0``)
    and ``sg_format --format`` / ``sg_dd of=`` on one, each keeping
    its read-only form (``lvremove`` / ``vgremove --test``, bare
    ``sg_format``, ``sg_dd`` reading a device to a file) — including
    ``lvreduce`` and shrink forms of ``lvresize`` (``-L`` / ``-l``
    without a leading ``+``), ``dcfldd`` / ``dc3dd`` ``of=`` on one,
    and ``ddrescue`` with a raw-device outfile, each keeping its
    grow / read-only form (``lvresize -L +1G``, ``lvreduce`` /
    ``lvresize --test``, ``dcfldd`` reading a device to a file,
    ``ddrescue`` to a regular file) — including the remaining
    sg3_utils write tools (``sg_write_same`` with one of ``--in`` /
    ``--lba`` / ``--num``, ``sg_unmap``, ``sg_write_buffer``,
    ``sg_write_long``) and ``dd_rescue`` with a raw-device outfile
    (copy mode) or shred target (``-2`` / ``-3`` / ``-4`` / ``-z`` /
    ``-Z``), each keeping its dry run / safety interlock
    (``sg_unmap`` / ``sg_write_buffer -d`` / ``--dry-run``,
    bare ``sg_write_same``, ``dd_rescue`` to a regular file) —
    including MTD flash tools erasing or writing a flash device
    (``flash_erase`` / ``flash_eraseall``, ``nandwrite``,
    ``flashcp``, ``ubiformat``, ``mtd_debug write`` / ``erase``),
    each keeping its read-only form (``nanddump``, ``mtdinfo``,
    ``flash_info``, ``mtd_debug info`` / ``read``, the read-only
    ``mtdNro`` nodes) — and the UBI volume tools on top of MTD
    (``ubiupdatevol`` writing an image into a volume, ``ubirmvol``
    removing one, ``ubimkvol`` creating one, ``ubirsvol`` shrinking
    one, ``ubidetach`` tearing the device down, ``ubinize -o``
    writing its image onto one), each keeping its read-only / grow
    form (``ubiattach``, ``ubinfo``, ``ubirsvol -s +…``,
    ``ubinize -o`` to a regular file) — and the MTD flash devices (``/dev/mtd*`` /
    ``mtdblock*`` / ``ubi*``), s390 DASD disks (``/dev/dasd*``)
    and Xen virtual disks (``/dev/xvd*``) as raw-device spellings
    in the copy / wipe / redirect checks — including
    dot-segment /
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

    The 0.6.40 audit round closed the remaining classes: the scan
    now fails closed when tokenization fails (a ``#`` comment with
    an apostrophe, ``rm -rf / # don't``, no longer waves every
    token-stage check through); line continuations and ``$IFS``
    are normalized before any pattern runs, and ``dd`` has a
    token-stage check, so a split ``dd`` command is priced like the
    joined one; ``rm`` recursion accepts ``-R`` as well as ``-r``;
    the guarded set names the system trees (``/etc``, ``/usr``,
    ``/var``, ``/opt``, ``/srv``, ``/boot``, ``/bin``, ``/sbin``,
    ``/lib``, ``/lib64``, ``/sys``, ``/proc``, ``/dev``, ``/root``)
    instead of deriving protection from passwd alone; same-command
    assignments, ``${VAR:-default}`` expansions and ``$USER`` /
    ``$LOGNAME`` path components are priced before the verb checks,
    a recursive forced delete of one bare unresolved variable is
    refused, and ``xargs`` feeding a recursive ``rm`` or a device
    ``dd`` is refused; destructive targets spelled through symlinks
    are resolved with ``realpath`` for the copy / wipe / ``dd`` /
    ``rm`` / ``find`` checks; interpreter one-liners (``python -c``,
    ``perl -e``, ``ruby -e``, ``node -e``, ``php -r``, ``lua -e``,
    ``awk`` programs using ``system(``) have their code argument
    scanned, and ``ssh`` remote commands are rescanned; any pipe
    into a shell, ``at`` or a crontab install is refused (not just
    the curl/wget download form), script files a shell is asked to
    run (and ``source`` files) are scanned before they run, and
    ``shell -s`` with a file redirect is refused; the NVMe write
    family (``write``, ``write-zeroes``, ``write-uncorrectable``,
    ``dsm --ad``, ``delete-ns``), ``sg_sanitize``, ``fio`` device
    writes, ``fallocate`` discard modes on a device and ``scrub``
    on a device are covered; and the availability spellings —
    ``init``/``telinit`` runlevels 0/6, ``kill`` of PID 1, writes
    to ``/proc/sysrq-trigger``, and fork bombs under any function
    name — are refused.
    """
    command = _normalize_command(_expand_ansi_c_quoting(command))
    patterns = [
        (r":\(\)\s*\{", "fork bomb"),
        # The fork-bomb pattern above only knew the literal name
        # ``:``; any renamed self-recursive function whose body
        # pipes or backgrounds a call to itself is the same bomb
        # (``x(){ x|x& };x``), so the name is captured and required
        # again inside the body, followed by a pipe or ``&``. A
        # recursive function that merely calls itself with arguments
        # (``walk(){ walk "$1"; }``) does not match.
        (r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{[^}]*\b\1\s*(?:\||&)",
         "fork bomb"),
        (r"\bmkfs(\.\w+)?\b", "filesystem format"),
        # A redirect onto the SysRq trigger forces an immediate
        # unsynced reboot / crash / poweroff (``echo b >
        # /proc/sysrq-trigger``); the tee / copy / dd spellings are
        # priced at the token stage (``_power_reason`` /
        # ``_dd_reason``).
        (r">>?\|?\s*['\"]?/+proc/sysrq-trigger\b", "power control"),
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
        # Fail closed. The old code returned None here on the theory
        # that the shell would report the syntax error — but a `#`
        # comment makes the shell *ignore* the very text shlex chokes
        # on: ``rm -rf / # don't`` tokenizes as an error for shlex
        # (the apostrophe opens a quote that never closes) while
        # bash runs the delete and treats the rest as a comment, so
        # every token-stage check was defeated at once (verified
        # against ``_is_dangerous`` before this fix). Retrying with
        # comments enabled prices the command the way the shell
        # does; a command that still cannot be tokenized goes to the
        # loose destructive-marker scan, never to a free pass.
        try:
            tokens = shlex.split(command, posix=True, comments=True)
        except ValueError:
            return _unparseable_reason(command)
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
    # Price same-command assignments and default-value expansions
    # (``T=/; rm -rf $T``, ``rm -rf ${D:-/}``) before the verb checks:
    # the shell evaluates them first, so the scan does too.
    tokens = _resolve_simple_assignments(tokens)
    return (_rm_reason(tokens) or _find_reason(tokens)
            or _chmod_reason(tokens) or _tree_permission_reason(tokens)
            or _raw_device_reason(tokens)
            or _device_wipe_reason(tokens)
            or _format_device_reason(tokens)
            or _archive_device_reason(tokens)
            or _image_device_reason(tokens)
            or _mtd_device_reason(tokens)
            or _ubi_device_reason(tokens)
            or _dd_reason(tokens)
            or _pipe_reason(tokens)
            or _power_reason(tokens)
            or _xargs_reason(tokens)
            or _interpreter_reason(tokens, _depth)
            or _script_file_reason(tokens, _depth)
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


def _resolve_public_ips(host: str) -> list[str]:
    """Resolve *host* and return its addresses, all verified public.

    Raises :exc:`ValueError` when the host does not resolve, or when
    *any* resolved address is loopback / private / link-local /
    multicast / reserved / unspecified — a name that answers with a
    mix is refused outright rather than partially trusted. This is
    the single resolution the SSRF guard validates and the fetch
    connections pin (see ``_PinnedHTTPConnection``), so the address
    that was checked is the address that is dialed.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Could not resolve host: {host!r}")
    ips: list[str] = []
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
        ips.append(sockaddr[0])
    if not ips:
        raise ValueError(f"Could not resolve host: {host!r}")
    return ips


def _assert_public_host(host: str) -> None:
    """Refuse loopback/private/link-local/etc. hosts (SSRF guard).

    Resolves ``host`` and raises :exc:`ValueError` when any resolved
    address is non-public, or when the host does not resolve at all.
    Shared by the ``fetch_url`` tool and the browser engine so both
    enforce the same boundary.
    """
    _resolve_public_ips(host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that dials a pre-validated address.

    The hostname is kept on the connection (so the ``Host`` header
    is the real one); only the TCP target is pinned to
    ``_pinned_ip`` — an address ``_resolve_public_ips`` returned for
    this host during this very request. DNS therefore gets no second
    say between validation and connect, which closes the
    DNS-rebinding TOCTOU in the old flow (validate one resolution,
    let urllib resolve again to connect). The connection is only
    built for direct requests: when a proxy is configured urllib
    routes through it and name resolution happens at the proxy, as
    before — the handlers below fall back to the stock classes then.
    """

    def __init__(self, *args, _pinned_ip: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = _pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout,
            self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The HTTPS twin: TLS is wrapped with the real hostname, so
    SNI and certificate verification check the name the user asked
    for — only the dialed address is pinned."""

    def __init__(self, *args, _pinned_ip: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = _pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout,
            self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._tunnel_host:
            self._tunnel()
        if self._context:
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host)


def _host_of(host_header: str) -> str:
    """The bare host of a ``req.host`` value (strips any port and
    the brackets of an IPv6 literal)."""
    if host_header.startswith("["):
        end = host_header.find("]")
        return host_header[1:end] if end != -1 else host_header
    return host_header.split(":", 1)[0]


def _pinned_factory(conn_class, host_header: str):
    """A do_open-compatible factory pinning *host_header*'s address.

    Resolution happens here, per request (and therefore per redirect
    hop): the addresses are re-validated and the first one is pinned
    into the connection. A validation failure surfaces as the same
    ValueError ``_validate_url`` raises.
    """
    pinned_ip = _resolve_public_ips(_host_of(host_header))[0]

    def factory(host_, timeout=None, **kwargs):
        return conn_class(host_, timeout=timeout,
                          _pinned_ip=pinned_ip, **kwargs)

    return factory


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler whose connections dial the validated address."""

    def http_open(self, req):
        if req.has_proxy():
            return super().http_open(req)
        return self.do_open(
            _pinned_factory(_PinnedHTTPConnection, req.host), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler whose connections dial the validated address."""

    def https_open(self, req):
        if req.has_proxy():
            return super().https_open(req)
        return self.do_open(
            _pinned_factory(_PinnedHTTPSConnection, req.host), req,
            context=self._context, check_hostname=self._check_hostname)


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
    """GET a URL and return its text. Size-capped, no credentials in logs.

    Connections are pinned to the address the SSRF guard validated
    (``_PinnedHTTPHandler`` / ``_PinnedHTTPSHandler``): the name is
    resolved once per request, every answer must be public, and the
    socket dials that answer — a DNS server that changes its reply
    between the check and the connect (rebinding) reaches nothing.
    """
    try:
        _validate_url(url)
    except ValueError as exc:
        return f"ERROR: {exc}"
    opener = urllib.request.build_opener(
        _RedirectGuard, _PinnedHTTPHandler(), _PinnedHTTPSHandler())
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
    except ValueError as exc:
        # The pinned handlers re-validate at connect time; a host
        # whose answers turned non-public since _validate_url (a
        # rebinding attempt) is refused with the guard's own message.
        return f"ERROR: {exc}"
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
