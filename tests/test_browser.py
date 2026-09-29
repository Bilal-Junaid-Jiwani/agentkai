"""Tests for agentkai.browser — real Chromium engine + agent tools.

The Playwright layer is faked: no browser, no network. Real-Chromium tests
are marked and skipped unless AGENTKAI_REAL_BROWSER=1.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentkai import browser as B
from agentkai.browser import BrowserSession, browser_tools
from agentkai.tools import PermissionDenied, ApprovalRequired

REAL_BROWSER = os.environ.get("AGENTKAI_REAL_BROWSER") == "1"
needs_real_browser = pytest.mark.skipif(
    not REAL_BROWSER, reason="needs real Chromium: AGENTKAI_REAL_BROWSER=1"
)


# ---- fake playwright layer -------------------------------------------------

class FakeHandle:
    def __init__(self, info):
        self._info = info
        self.clicked = False
        self.filled = None
        self.pressed_keys = []
        self.typed = None
        self.waited = False

    def evaluate(self, js):
        return self._info

    def click(self, timeout=None):
        self.clicked = True

    def fill(self, text, timeout=None):
        self.filled = text

    def press(self, key, timeout=None):
        self.pressed_keys.append(key)

    def type(self, text, timeout=None):
        self.typed = text

    def wait_for(self, timeout=None):
        self.waited = True


class FakeLocator:
    def __init__(self, handles):
        self._handles = handles

    def count(self):
        return len(self._handles)

    @property
    def first(self):
        return self._handles[0]

    def click(self, timeout=None):
        self.first.click(timeout=timeout)

    def fill(self, text, timeout=None):
        self.first.fill(text, timeout=timeout)

    def press(self, key, timeout=None):
        self.first.press(key, timeout=timeout)

    def wait_for(self, timeout=None):
        self.first.wait_for(timeout=timeout)


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class FakeDownload:
    def __init__(self, filename):
        self.suggested_filename = filename
        self.saved_to = None
        self.cancelled = False

    def save_as(self, path):
        self.saved_to = path
        Path(path).write_bytes(b"fake-download-bytes")

    def cancel(self):
        self.cancelled = True


class FakeExpectDownload:
    def __init__(self, download):
        self._download = download

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def value(self):
        return self._download


class FakePage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self.keyboard = FakeKeyboard()
        self.handles = [
            FakeHandle({"tag": "button", "type": "", "text": "Sign in",
                        "name": "", "href": "", "id": "login"}),
            FakeHandle({"tag": "input", "type": "text", "text": "",
                        "name": "q", "href": "", "id": ""}),
        ]
        self.selector_map = {"#login": self.handles[0]}
        self.text_map = {"Sign in": [self.handles[0]]}
        self.body_text = "Example page body text. " * 10
        self.next_download_filename = "report.pdf"
        self.last_download = None
        self.went_back = False
        self.went_forward = False
        self.reloaded = False
        self.closed = False
        self.waited_selector = None

    def title(self):
        return "Fake Title"

    def goto(self, url, wait_until=None):
        self.url = url

    def go_back(self):
        self.went_back = True
        self.url = "https://example.com/prev"

    def go_forward(self):
        self.went_forward = True

    def reload(self, wait_until=None):
        self.reloaded = True

    def close(self):
        self.closed = True

    def query_selector_all(self, selector):
        return list(self.handles)

    def query_selector(self, selector):
        return self.selector_map.get(selector)

    def get_by_text(self, text):
        return FakeLocator(self.text_map.get(text, []))

    def inner_text(self, selector):
        return self.body_text

    def screenshot(self, path=None):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def wait_for_selector(self, selector, timeout=None):
        self.waited_selector = selector

    def wait_for_load_state(self, state, timeout=None):
        pass

    def expect_download(self):
        dl = FakeDownload(self.next_download_filename)
        self.last_download = dl
        return FakeExpectDownload(dl)


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.launch_kwargs = {}
        self.closed = False

    def new_page(self):
        page = FakePage(url="about:blank")
        self.pages.append(page)
        return page

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.contexts = []

    def launch_persistent_context(self, *args, **kwargs):
        ctx = FakeContext()
        ctx.launch_kwargs = kwargs
        self.contexts.append(ctx)
        return ctx


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture()
def session():
    s = BrowserSession(playwright_factory=lambda: FakePlaywright())
    yield s
    s.close()


@pytest.fixture()
def tools(session):
    return {t.name: t for t in browser_tools(session)}


# ---- lifecycle -------------------------------------------------------------

def test_lazy_start(session):
    assert session.started is False
    session.ensure_started()
    assert session.started is True


def test_persistent_profile_used():
    captured = {}

    class CapChromium(FakeChromium):
        def launch_persistent_context(self, *args, **kwargs):
            captured.update(kwargs)
            return super().launch_persistent_context(*args, **kwargs)

    class CapPW(FakePlaywright):
        def __init__(self):
            self.chromium = CapChromium()

    s = BrowserSession(playwright_factory=lambda: CapPW())
    s.ensure_started()
    try:
        assert captured["accept_downloads"] is True
        assert "downloads_path" in captured
        assert s.headless is True
    finally:
        s.close()


def test_missing_playwright_gives_helpful_error(monkeypatch):
    def _boom():
        raise B.BrowserUnavailable("playwright is not installed. Run: X")
    monkeypatch.setattr(B, "_require_playwright_factory", _boom)
    s = BrowserSession()
    tool = browser_tools(s)[0]
    result = tool.run(url="https://example.com/")
    assert result.startswith("ERROR:")
    assert "playwright" in result


# ---- navigation ------------------------------------------------------------

def test_goto_ok(session, tools):
    out = tools["browser_goto"].run(url="https://example.com/")
    assert out["url"] == "https://example.com/"
    assert out["title"] == "Fake Title"


def test_goto_blocks_file_url(tools):
    out = tools["browser_goto"].run(url="file:///etc/passwd")
    assert out.startswith("ERROR:")
    assert "file" in out


def test_goto_blocks_javascript_url(tools):
    out = tools["browser_goto"].run(url="javascript:alert(1)")
    assert out.startswith("ERROR:")


def test_goto_blocks_embedded_credentials(tools):
    out = tools["browser_goto"].run(url="https://user:pass@example.com/")
    assert out.startswith("ERROR:")
    assert "credentials" in out


def test_goto_rejects_empty(tools):
    out = tools["browser_goto"].run(url="   ")
    assert out.startswith("ERROR:")


def test_back(session, tools):
    out = tools["browser_back"].run()
    assert out["url"] == "https://example.com/prev"
    assert session._page.went_back is True


def test_forward(session):
    out = session.forward()
    assert session._page.went_forward is True
    assert out["url"]


# ---- snapshot / read -------------------------------------------------------

def test_snapshot_lists_refs(session, tools):
    out = tools["browser_read"].run()
    assert "[e1] <button> Sign in" in out["elements"]
    assert "[e2] <input>" in out["elements"]
    assert "Example page body text" in out["text"]
    assert "hint" in out


def test_snapshot_invalidates_refs_on_navigation(session):
    session.snapshot()
    assert "e1" in session._refs
    session.goto("https://example.com/other")
    assert session._refs == {}


# ---- acting ----------------------------------------------------------------

def test_click_by_ref(session, tools):
    session.snapshot()
    out = tools["browser_click"].run(target="e1")
    assert out["ok"] is True
    assert session._page.handles[0].clicked is True


def test_click_by_css_selector(session, tools):
    out = tools["browser_click"].run(target="#login")
    assert out["ok"] is True
    assert session._page.handles[0].clicked is True


def test_click_by_visible_text(session, tools):
    out = tools["browser_click"].run(target="Sign in")
    assert out["ok"] is True
    assert session._page.handles[0].clicked is True


def test_click_unknown_target_is_error(session, tools):
    session.snapshot()
    out = tools["browser_click"].run(target="no-such-thing-xyz")
    assert out.startswith("ERROR:")
    assert "snapshot" in out


def test_click_empty_target_is_error(tools):
    out = tools["browser_click"].run(target="  ")
    assert out.startswith("ERROR:")


def test_fill_and_submit(session, tools):
    session.snapshot()
    out = tools["browser_fill"].run(target="e2", text="hello", submit=True)
    assert out["ok"] is True
    assert out["submitted"] is True
    handle = session._page.handles[1]
    assert handle.filled == "hello"
    assert handle.pressed_keys == ["Enter"]


def test_fill_by_snapshot_label(session, tools):
    # "q" is the input's name/placeholder label, not visible text or a ref
    session.snapshot()
    out = tools["browser_fill"].run(target="q", text="hello")
    assert out["ok"] is True
    assert session._page.handles[1].filled == "hello"


def test_http_form_submit_warns(session, tools, capsys):
    session.ensure_started()
    session._page.url = "http://example.com/login"
    session.snapshot()
    tools["browser_fill"].run(target="e2", text="x", submit=True)
    err = capsys.readouterr().err
    assert "WARNING" in err and "http://" in err


def test_https_form_submit_no_warning(session, tools, capsys):
    session.snapshot()
    tools["browser_fill"].run(target="e2", text="x", submit=True)
    assert capsys.readouterr().err == ""


def test_press_key_on_page(session):
    out = session.press("Escape")
    assert out["ok"] is True
    assert session._page.keyboard.pressed == ["Escape"]


def test_wait_for_text(session):
    out = session.wait_for(text="Sign in")
    assert out["ok"] is True
    assert session._page.handles[0].waited is True


def test_wait_for_selector(session):
    out = session.wait_for(selector="#login")
    assert out["ok"] is True
    assert session._page.waited_selector == "#login"


# ---- screenshots / downloads -----------------------------------------------

def test_screenshot(session, tools, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "SCREENSHOT_DIR", tmp_path / "shots")
    out = tools["browser_screenshot"].run()
    p = Path(out["path"])
    assert p.parent == tmp_path / "shots"
    assert p.suffix == ".png" and p.stat().st_size > 0


def test_download_allowlisted(session, tools, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DOWNLOAD_DIR", tmp_path / "dl")
    session.snapshot()
    out = tools["browser_download"].run(target="e1")
    p = Path(out["path"])
    assert p.parent == tmp_path / "dl"
    assert p.suffix == ".pdf" and p.stat().st_size > 0


def test_download_blocked_executable(session, tools, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DOWNLOAD_DIR", tmp_path / "dl")
    session.ensure_started()
    session._page.next_download_filename = "setup.exe"
    session.snapshot()
    out = tools["browser_download"].run(target="e1")
    assert out.startswith("ERROR:")
    assert "allowlist" in out
    assert session._page.last_download.cancelled is True


def test_download_unique_naming(session, tools, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DOWNLOAD_DIR", tmp_path / "dl")
    session.snapshot()
    first = tools["browser_download"].run(target="e1")["path"]
    second = tools["browser_download"].run(target="e1")["path"]
    assert first != second
    assert Path(second).name.startswith("report-1")


# ---- tabs ------------------------------------------------------------------

def test_new_tab_switch_close(session):
    r = session.new_tab("https://example.com/a")
    assert r["tab"] == 1
    assert len(session._pages) == 2
    session.switch_tab(0)
    assert session._page is session._pages[0]
    out = session.close_tab(1)
    assert out["tabs"] == 1


def test_cannot_close_last_tab(session):
    out = session.close()
    assert out is None
    s2 = BrowserSession(playwright_factory=lambda: FakePlaywright())
    s2.ensure_started()
    with pytest.raises(B.BrowserError):
        s2.close_tab(0)
    s2.close()


# ---- tool registry / risks / gates ------------------------------------------

EXPECTED_RISKS = {
    "browser_goto": "medium",
    "browser_read": "medium",
    "browser_click": "high",
    "browser_fill": "high",
    "browser_screenshot": "medium",
    "browser_download": "high",
    "browser_back": "medium",
}


def test_tool_names_and_risks(tools):
    assert set(tools) == set(EXPECTED_RISKS)
    for name, risk in EXPECTED_RISKS.items():
        assert tools[name].risk == risk, name


def test_tool_schemas_are_flat(tools):
    for tool in tools.values():
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == tool.name


def test_gate_deny_raises(tools):
    with pytest.raises(PermissionDenied):
        tools["browser_click"].run(
            _gate=lambda action: "deny", target="e1")


def test_gate_ask_raises_approval(tools):
    with pytest.raises(ApprovalRequired):
        tools["browser_goto"].run(
            _gate=lambda action: "ask", url="https://example.com/")


def test_gate_sees_tool_risk_and_args(tools):
    seen = {}

    def gate(action):
        seen.update(action)
        return "allow"

    tools["browser_fill"].run(_gate=gate, target="e1", text="x")
    assert seen["tool"] == "browser_fill"
    assert seen["risk"] == "high"
    assert seen["args"]["text"] == "x"


# ---- real browser (opt-in) ---------------------------------------------------

@pytest.mark.real_browser
@needs_real_browser
def test_real_goto_and_snapshot(tmp_path):
    pytest.importorskip("playwright")
    s = BrowserSession(headless=True)
    try:
        s.ensure_started()
        out = s.goto("https://example.com/")
        assert "example.com" in out["url"]
        snap = s.snapshot()
        assert snap["title"]
        shot = s.screenshot()
        assert Path(shot["path"]).stat().st_size > 0
    finally:
        s.close()


@pytest.mark.real_browser
@needs_real_browser
def test_real_selector_resolution():
    pytest.importorskip("playwright")
    s = BrowserSession(headless=True)
    try:
        s.ensure_started()
        s.goto("https://example.com/")
        s.snapshot()
        # example.com has a single link; resolve it by visible text
        s.click("More information")
        assert "iana.org" in s._page.url
    finally:
        s.close()
