"""Real Chromium browser engine + agent tools (Playwright-based).

The browser is the agent's hands on the web: navigation, reading pages,
clicking, filling forms, screenshots, and downloads — with logins persisted
in the user's own profile directory (``~/.agentkai/browser-profile``), so
cookies and sessions survive restarts and are never exfiltrated anywhere.

Safety model:
- Only ``http``/``https`` URLs are allowed. ``file://`` and every other
  scheme (``about:``, ``data:``, ``javascript:``, ``chrome:``, ...) are
  rejected before anything loads.
- Form submits over plain ``http`` print a stderr warning.
- Downloads are restricted to an allowlist of document/media/archive
  extensions; executables and scripts are refused.
- Every action has a timeout; nothing here can hang forever.

Playwright is an optional dependency: importing this module never imports
playwright. The browser starts lazily on first use, and a clear error is
returned when playwright (or its Chromium) is not installed::

    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .tools import Tool

__all__ = [
    "BrowserSession",
    "browser_tools",
    "BrowserError",
    "BrowserUnavailable",
    "PROFILE_DIR",
    "DOWNLOAD_DIR",
    "SCREENSHOT_DIR",
    "DOWNLOAD_ALLOWLIST",
]

# ---- locations -------------------------------------------------------------

_AGENTKAI_HOME = Path("~/.agentkai").expanduser()
PROFILE_DIR = _AGENTKAI_HOME / "browser-profile"
DOWNLOAD_DIR = _AGENTKAI_HOME / "downloads"
SCREENSHOT_DIR = _AGENTKAI_HOME / "screenshots"

# ---- timeouts (ms for playwright, s where noted) ----------------------------

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
WAIT_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_S = 120
SNAPSHOT_TEXT_CHARS = 4_000

# ---- safety ----------------------------------------------------------------

_BLOCKED_SCHEMES = frozenset({
    "file", "about", "data", "javascript", "chrome", "chrome-extension",
    "view-source", "blob", "ftp", "mailto", "tel",
})

#: Extensions the agent is allowed to save to disk. Anything else —
#: executables, scripts, disk images — is refused with an ERROR.
DOWNLOAD_ALLOWLIST = frozenset({
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".ics", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".m4a",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
})

_INTERACTIVE_SELECTOR = (
    "a, button, input, select, textarea, summary, "
    '[role="button"], [role="link"], [role="textbox"], '
    '[role="checkbox"], [role="radio"], [role="switch"], [onclick]'
)

#: Runs inside the page for every interactive element. Returns a small dict
#: describing it. (Evaluated per-element so handles and info stay 1:1.)
_ELEMENT_INFO_JS = r"""(el) => {
  const text = (el.innerText || el.value || el.getAttribute('aria-label')
    || el.getAttribute('placeholder') || el.getAttribute('title') || '')
    .trim().replace(/\s+/g, ' ').slice(0, 80);
  return {
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    text: text,
    name: el.getAttribute('name') || '',
    href: el.getAttribute('href') || '',
    id: el.id || '',
  };
}"""


class BrowserError(Exception):
    """A browser action failed (bad URL, missing element, timeout, ...)."""


class BrowserUnavailable(BrowserError):
    """Playwright / Chromium is not installed."""


def _require_playwright_factory() -> Callable[[], Any]:
    """Import playwright lazily; raise a helpful error when missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "playwright is not installed. Run: "
            "pip install playwright && playwright install chromium"
        ) from exc
    return lambda: sync_playwright().start()


def _validate_url(url: str) -> str:
    """Allow only http/https. Reject credentials-in-URL and empty hosts."""
    url = (url or "").strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise BrowserError(
            f"blocked URL scheme {scheme or '(none)'!r}: "
            "only http:// and https:// pages can be opened "
            "(file:// and app schemes are never allowed)"
        )
    if not parsed.hostname:
        raise BrowserError(f"URL has no host: {url!r}")
    if parsed.username or parsed.password:
        # Never let credentials reach history, logs, or the wire via us.
        raise BrowserError("URLs with embedded credentials are not allowed")
    return url


def _unique_path(directory: Path, filename: str) -> Path:
    """Pick a non-colliding path inside *directory* for *filename*."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:80] or "download"
    stem, suffix = Path(safe).stem, Path(safe).suffix.lower()
    candidate = directory / f"{stem}{suffix}"
    i = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{i}{suffix}"
        i += 1
    return candidate


# ---- session ---------------------------------------------------------------

class BrowserSession:
    """A persistent Chromium session owned by the agent.

    Args:
        headless: run without a visible window (default). Pass
            ``headless=False`` to watch the agent work.
        playwright_factory: override for tests. A zero-arg callable
            returning a started playwright-like object with a ``chromium``
            attribute exposing ``launch_persistent_context``.
    """

    def __init__(
        self,
        headless: bool = True,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.headless = headless
        self._factory = playwright_factory
        self._pw: Any = None
        self._context: Any = None
        self._pages: list[Any] = []
        self._page: Any = None
        self._refs: dict[str, Any] = {}  # "e12" -> element handle/locator
        self._ref_labels: dict[str, str] = {}  # "e12" -> searchable label

    # -- lifecycle ------------------------------------------------------

    def ensure_started(self) -> None:
        """Launch Chromium (persistent profile) if not already running."""
        if self._page is not None:
            return
        factory = self._factory or _require_playwright_factory()
        try:
            self._pw = factory()
        except BrowserUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            raise BrowserUnavailable(
                f"could not start playwright: {exc}. "
                "Run `playwright install chromium`?"
            ) from exc
        for d in (PROFILE_DIR, DOWNLOAD_DIR, SCREENSHOT_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=self.headless,
            accept_downloads=True,
            downloads_path=str(DOWNLOAD_DIR),
            viewport={"width": 1280, "height": 800},
        )
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        pages = list(self._context.pages)
        self._page = pages[0] if pages else self._context.new_page()
        self._pages = [self._page]

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._context = self._page = None
        self._pages = []
        self._refs = {}

    def __enter__(self) -> "BrowserSession":
        self.ensure_started()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def started(self) -> bool:
        return self._page is not None

    # -- navigation -----------------------------------------------------

    def _navigated(self) -> None:
        """Call after any navigation: refs from the old page are stale."""
        self._refs = {}
        self._ref_labels = {}

    def goto(self, url: str) -> dict:
        """Navigate to an http(s) URL. Returns url + title."""
        self.ensure_started()
        url = _validate_url(url)
        self._page.goto(url, wait_until="domcontentloaded")
        self._navigated()
        return {"url": self._page.url, "title": self._page.title()}

    def back(self) -> dict:
        self.ensure_started()
        self._page.go_back()
        self._navigated()
        return {"url": self._page.url, "title": self._page.title()}

    def forward(self) -> dict:
        self.ensure_started()
        self._page.go_forward()
        self._navigated()
        return {"url": self._page.url, "title": self._page.title()}

    def reload(self) -> dict:
        self.ensure_started()
        self._page.reload(wait_until="domcontentloaded")
        self._navigated()
        return {"url": self._page.url, "title": self._page.title()}

    # -- tabs -----------------------------------------------------------

    def new_tab(self, url: str | None = None) -> dict:
        """Open a new tab, make it active. Returns its index + url."""
        self.ensure_started()
        page = self._context.new_page()
        self._pages.append(page)
        self._page = page
        self._navigated()
        if url:
            return {"tab": len(self._pages) - 1, **self.goto(url)}
        return {"tab": len(self._pages) - 1, "url": page.url}

    def switch_tab(self, index: int) -> dict:
        self.ensure_started()
        if not 0 <= index < len(self._pages):
            raise BrowserError(
                f"no tab {index} (have {len(self._pages)} tabs)"
            )
        self._page = self._pages[index]
        self._navigated()
        return {"tab": index, "url": self._page.url,
                "title": self._page.title()}

    def close_tab(self, index: int) -> dict:
        self.ensure_started()
        if len(self._pages) <= 1:
            raise BrowserError("cannot close the last tab")
        if not 0 <= index < len(self._pages):
            raise BrowserError(f"no tab {index}")
        self._pages[index].close()
        del self._pages[index]
        if self._page not in self._pages:
            self._page = self._pages[0]
        self._navigated()
        return {"tabs": len(self._pages), "url": self._page.url}

    # -- reading --------------------------------------------------------

    def snapshot(self) -> dict:
        """Accessible snapshot: interactive elements with refs + page text.

        Refs (``e1``, ``e2``, ...) can be passed to click/fill/press.
        Refreshing the snapshot (or navigating) invalidates old refs.
        """
        self.ensure_started()
        handles = self._page.query_selector_all(_INTERACTIVE_SELECTOR)
        elements: list[dict] = []
        self._refs = {}
        self._ref_labels = {}
        for i, handle in enumerate(handles, start=1):
            try:
                info = handle.evaluate(_ELEMENT_INFO_JS)
            except Exception:  # noqa: BLE001 - stale handle; skip it
                continue
            ref = f"e{i}"
            self._refs[ref] = handle
            label = info.get("text") or info.get("name") or info.get("id") or ""
            detail = info.get("type") or info.get("href") or ""
            self._ref_labels[ref] = f"{label} {detail}".strip().lower()
            elements.append({
                "ref": ref,
                "tag": info.get("tag", "?"),
                "label": label,
                "detail": detail[:60],
            })
        try:
            text = self._page.inner_text("body")
        except Exception:  # noqa: BLE001 - some pages have no body text
            text = ""
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "elements": elements,
            "text": text[:SNAPSHOT_TEXT_CHARS],
            "text_truncated": len(text) > SNAPSHOT_TEXT_CHARS,
        }

    def read(self, max_chars: int = SNAPSHOT_TEXT_CHARS) -> dict:
        """Return the current page's visible text (truncated)."""
        self.ensure_started()
        try:
            text = self._page.inner_text("body")
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"could not read page text: {exc}") from exc
        limit = max(256, min(int(max_chars), 50_000))
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "text": text[:limit],
            "truncated": len(text) > limit,
        }

    # -- acting ---------------------------------------------------------

    def _resolve(self, target: str) -> Any:
        """Resolve *target* to something clickable/fillable.

        Order: snapshot ref (``e12``) → CSS selector → snapshot label
        (matches placeholder/aria-label/text seen in the last snapshot) →
        visible text.
        """
        self.ensure_started()
        target = (target or "").strip()
        if not target:
            raise BrowserError("empty target: pass a ref (e1), CSS selector, or text")
        if target in self._refs:
            return self._refs[target]
        el = self._page.query_selector(target)
        if el is not None:
            return el
        want = target.lower()
        for ref, label in self._ref_labels.items():
            if label and (want in label or label in want):
                return self._refs[ref]
        loc = self._page.get_by_text(target)
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:  # noqa: BLE001 - get_by_text not supported; fall through
            pass
        raise BrowserError(
            f"no element for {target!r}: take a fresh snapshot "
            "(refs expire after navigation)"
        )

    def click(self, target: str) -> dict:
        """Click an element by ref, CSS selector, or visible text."""
        el = self._resolve(target)
        el.click(timeout=ACTION_TIMEOUT_MS)
        return {"ok": True, "url": self._page.url}

    def fill(self, target: str, text: str, submit: bool = False) -> dict:
        """Fill a text field. With ``submit=True`` presses Enter afterwards."""
        el = self._resolve(target)
        el.fill(text, timeout=ACTION_TIMEOUT_MS)
        if submit:
            self._warn_if_http_form()
            el.press("Enter", timeout=ACTION_TIMEOUT_MS)
        return {"ok": True, "submitted": submit, "url": self._page.url}

    def press(self, key: str, target: str | None = None) -> dict:
        """Press a key: on an element (by ref/selector/text) or the page."""
        self.ensure_started()
        if target:
            self._resolve(target).press(key, timeout=ACTION_TIMEOUT_MS)
        else:
            self._page.keyboard.press(key)
        return {"ok": True, "key": key}

    def type(self, target: str, text: str) -> dict:
        """Type text keystroke-by-keystroke (for JS-heavy inputs)."""
        el = self._resolve(target)
        el.type(text, timeout=ACTION_TIMEOUT_MS)
        return {"ok": True, "url": self._page.url}

    def _warn_if_http_form(self) -> None:
        if urlparse(self._page.url).scheme == "http":
            print(
                "WARNING: submitting a form over unencrypted http:// "
                f"{self._page.url} — credentials could be sniffed.",
                file=sys.stderr,
            )

    def wait_for(
        self,
        text: str | None = None,
        selector: str | None = None,
        timeout: int = WAIT_TIMEOUT_MS // 1000,
    ) -> dict:
        """Wait for text/selector to appear. Returns when it does."""
        self.ensure_started()
        ms = max(1, min(int(timeout), 300)) * 1000
        if selector:
            self._page.wait_for_selector(selector, timeout=ms)
        elif text:
            self._page.get_by_text(text).first.wait_for(timeout=ms)
        else:
            self._page.wait_for_load_state("networkidle", timeout=ms)
        return {"ok": True, "url": self._page.url}

    # -- screenshots & downloads ----------------------------------------

    def screenshot(self, path: str | None = None) -> dict:
        """Screenshot the current page. Returns the saved PNG path."""
        self.ensure_started()
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        if path:
            dest = _unique_path(SCREENSHOT_DIR, Path(path).name)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = _unique_path(SCREENSHOT_DIR, f"screenshot-{stamp}.png")
        self._page.screenshot(path=str(dest))
        return {"path": str(dest)}

    def click_and_download(
        self, target: str, timeout: int = DOWNLOAD_TIMEOUT_S
    ) -> dict:
        """Click an element and save the download it triggers.

        Only allowlisted file types are saved; anything else is cancelled
        and refused with an ERROR.
        """
        self.ensure_started()
        el = self._resolve(target)
        with self._page.expect_download() as dl_info:
            el.click(timeout=ACTION_TIMEOUT_MS)
        download = dl_info.value
        filename = download.suggested_filename or "download"
        ext = Path(filename).suffix.lower()
        if ext not in DOWNLOAD_ALLOWLIST:
            try:
                download.cancel()
            except Exception:  # noqa: BLE001 - best effort
                pass
            raise BrowserError(
                f"refusing download of {filename!r}: "
                f"extension {ext or '(none)'!r} is not in the download allowlist"
            )
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(DOWNLOAD_DIR, filename)
        download.save_as(str(dest))
        return {"path": str(dest), "filename": Path(dest).name}


# ---- agent tools -----------------------------------------------------------

def browser_tools(session: BrowserSession) -> list[Tool]:
    """Build the browser Tool list bound to *session*.

    The session starts lazily on first tool use. Tools never raise on bad
    input — failures come back as ``"ERROR: ..."`` via ``Tool.run`` —
    except permission decisions, which raise as usual.
    """

    def _goto(url: str) -> dict:
        return session.goto(url)

    def _read(max_chars: int = SNAPSHOT_TEXT_CHARS) -> dict:
        snap = session.snapshot()
        text = snap["text"]
        limit = max(256, min(int(max_chars), 50_000))
        lines = [
            f"[{e['ref']}] <{e['tag']}> {e['label']}"
            + (f" ({e['detail']})" if e["detail"] else "")
            for e in snap["elements"][:100]
        ]
        return {
            "url": snap["url"],
            "title": snap["title"],
            "elements": "\n".join(lines),
            "elements_truncated": len(snap["elements"]) > 100,
            "text": text[:limit],
            "text_truncated": snap["text_truncated"] or len(text) > limit,
            "hint": "click/fill with a ref like 'e3', a CSS selector, or visible text",
        }

    def _click(target: str) -> dict:
        return session.click(target)

    def _fill(target: str, text: str, submit: bool = False) -> dict:
        return session.fill(target, text, submit=bool(submit))

    def _screenshot() -> dict:
        return session.screenshot()

    def _download(target: str) -> dict:
        return session.click_and_download(target)

    def _back() -> dict:
        return session.back()

    return [
        Tool(
            name="browser_goto",
            description=(
                "Navigate the real Chromium browser to a URL "
                "(http/https only). Returns the page URL and title."
            ),
            json_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            risk="medium",
            func=_goto,
        ),
        Tool(
            name="browser_read",
            description=(
                "Read the current page: interactive elements listed with refs "
                "(e1, e2, ...) plus the visible page text."
            ),
            json_schema={
                "type": "object",
                "properties": {"max_chars": {"type": "integer"}},
            },
            risk="medium",
            func=_read,
        ),
        Tool(
            name="browser_click",
            description=(
                "Click a page element by snapshot ref (e.g. 'e3'), "
                "CSS selector, or visible text."
            ),
            json_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            risk="high",
            func=_click,
        ),
        Tool(
            name="browser_fill",
            description=(
                "Fill a text field by ref, CSS selector, or visible label. "
                "Set submit=true to press Enter afterwards."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                    "submit": {"type": "boolean"},
                },
                "required": ["target", "text"],
            },
            risk="high",
            func=_fill,
        ),
        Tool(
            name="browser_screenshot",
            description="Screenshot the current page; returns the saved PNG path.",
            json_schema={"type": "object", "properties": {}},
            risk="medium",
            func=_screenshot,
        ),
        Tool(
            name="browser_download",
            description=(
                "Click an element and save the file it downloads "
                "(documents/media/archives only); returns the saved path."
            ),
            json_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            risk="high",
            func=_download,
        ),
        Tool(
            name="browser_back",
            description="Go back one page in the browser history.",
            json_schema={"type": "object", "properties": {}},
            risk="medium",
            func=_back,
        ),
    ]
