"""Tests for the skills framework + bundled skills. No network anywhere."""
import base64
import json

import pytest

from agentkai.skills import (
    SkillError,
    SkillLoader,
    install_skill,
    list_skills,
    parse_skill_md,
    remove_skill,
)
from agentkai.skills_bundle._http import HttpClient, HttpError


# ---- fake HTTP ---------------------------------------------------------------

class FakeTransport:
    """Canned responder: routes = [(method, url_part, status, payload)]."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        for m, part, status, payload in self.routes:
            if m == method and part in url:
                raw = (payload if isinstance(payload, bytes)
                       else json.dumps(payload).encode())
                return status, {}, raw
        return 404, {}, b'{"message": "not mocked"}'


def skill_config(tmp_path, routes, token_env, token="test-token-abc"):
    return {"home": tmp_path,
            "transport": FakeTransport(routes)}


@pytest.fixture()
def home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    for var in ("GMAIL_TOKEN", "GCAL_TOKEN", "GITHUB_TOKEN", "SPOTIFY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


# ---- frontmatter parsing -----------------------------------------------------

def test_parse_skill_md_valid(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: demo\ndescription: Does things.\nversion: 1.2.3\n"
                 "when: things, stuff\n---\n\n# Demo\n\nDo things.\n")
    meta = parse_skill_md(p)
    assert meta["name"] == "demo"
    assert meta["version"] == "1.2.3"
    assert meta["when"] == "things, stuff"
    assert meta["instructions"].startswith("# Demo")


def test_parse_skill_md_missing_field(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: demo\ndescription: x\n---\nbody\n")
    with pytest.raises(SkillError, match="version"):
        parse_skill_md(p)


def test_parse_skill_md_no_frontmatter(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("# Just markdown\n")
    with pytest.raises(SkillError, match="frontmatter"):
        parse_skill_md(p)


def test_parse_skill_md_name_mismatch_rejected(home_env):
    d = home_env / "skills" / "wrongname"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: rightname\ndescription: x\nversion: 0.1.0\n---\nbody\n")
    with pytest.raises(SkillError, match="!="):
        SkillLoader().load("wrongname")


# ---- discovery / loader ------------------------------------------------------

def test_bundled_skills_discovered():
    names = [n for n, _, _ in SkillLoader().discover()]
    for expected in ("gmail", "google_calendar", "github", "spotify"):
        assert expected in names


def test_loader_tools_and_context(home_env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    loader = SkillLoader()
    skills = loader.all()
    assert {s.name for s in skills} >= {
        "gmail", "google_calendar", "github", "spotify"}
    tools = loader.tools()
    names = [t.name for t in tools]
    assert "gmail_send" in names
    assert "calendar_delete" in names
    assert "github_open_pr" in names
    assert "spotify_queue" in names
    assert len(names) == len(set(names)), "tool names must be unique"
    ctx = loader.system_context()
    assert "## Skill: gmail" in ctx
    assert "GMAIL_TOKEN" in ctx


def test_loader_match_keyword():
    matches = SkillLoader().match("send an email to my inbox")
    assert matches and matches[0].name == "gmail"
    matches = SkillLoader().match("what song is playing on spotify")
    assert matches and matches[0].name == "spotify"
    assert SkillLoader().match("") == []


def test_user_skill_shadows_bundled(home_env):
    d = home_env / "skills" / "gmail"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: gmail\ndescription: shadow\nversion: 9.9.9\n---\nshadow\n")
    skill = SkillLoader().load("gmail")
    assert skill.source == "user"
    assert skill.version == "9.9.9"


def test_skill_without_tools_py(home_env):
    d = home_env / "skills" / "notools"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: notools\ndescription: x\nversion: 0.1.0\n---\nbody\n")
    skill = SkillLoader().load("notools")
    assert skill.tools == []


# ---- install / remove --------------------------------------------------------

def _make_skill_dir(tmp_path, name="myskill"):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\nversion: 0.2.0\n---\nbody\n")
    return d


def test_install_from_local_dir(home_env, tmp_path):
    src = _make_skill_dir(tmp_path)
    dest = install_skill(str(src))
    assert dest.is_dir()
    assert (dest / "manifest.json").exists()
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["name"] == "myskill"
    assert manifest["version"] == "0.2.0"
    assert manifest["source"] == str(src)
    assert any(r["name"] == "myskill" for r in list_skills())


def test_install_duplicate_needs_force(home_env, tmp_path):
    src = _make_skill_dir(tmp_path)
    install_skill(str(src))
    with pytest.raises(SkillError, match="already installed"):
        install_skill(str(src))
    install_skill(str(src), force=True)  # overwrites fine


def test_install_prefers_frontmatter_name(home_env, tmp_path):
    src = tmp_path / "some-dir-name"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: realname\ndescription: x\nversion: 0.1.0\n---\nbody\n")
    dest = install_skill(str(src))
    assert dest.name == "realname"
    assert SkillLoader().load("realname").version == "0.1.0"


def test_install_rejects_non_skill(home_env, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(SkillError, match="no SKILL.md"):
        install_skill(str(d))
    assert not (home_env / "skills" / "empty").exists()


def test_install_rejects_bad_source(home_env):
    with pytest.raises(SkillError):
        install_skill("/does/not/exist")


def test_remove_skill(home_env, tmp_path):
    install_skill(str(_make_skill_dir(tmp_path)))
    remove_skill("myskill")
    assert not any(r["name"] == "myskill" for r in list_skills())


def test_remove_bundled_refused(home_env):
    with pytest.raises(SkillError, match="bundled"):
        remove_skill("gmail")


def test_remove_missing(home_env):
    with pytest.raises(SkillError, match="not installed"):
        remove_skill("nope")


# ---- http client -------------------------------------------------------------

def test_http_error_never_leaks_headers():
    t = FakeTransport([("GET", "/x", 401, {"message": "bad"})])
    c = HttpClient("https://example.com",
                   headers={"Authorization": "Bearer SUPERSECRET"},
                   transport=t)
    with pytest.raises(HttpError) as ei:
        c.request("GET", "/x")
    assert ei.value.status == 401
    assert "SUPERSECRET" not in str(ei.value)
    assert "Bearer" not in str(ei.value)
    # but the header WAS sent on the wire
    assert t.calls[0]["headers"]["Authorization"] == "Bearer SUPERSECRET"


# ---- gmail -------------------------------------------------------------------

def _gmail_tools(home_env, monkeypatch, routes):
    monkeypatch.setenv("GMAIL_TOKEN", "test-token-abc")
    from agentkai.skills_bundle.gmail.tools import get_tools
    transport = FakeTransport(routes)
    return get_tools({"home": home_env, "transport": transport}), transport


def test_gmail_search(home_env, monkeypatch):
    tools, transport = _gmail_tools(home_env, monkeypatch, [
        ("GET", "/users/me/messages", 200,
         {"messages": [{"id": "m1", "threadId": "t1"}],
          "resultSizeEstimate": 1})])
    out = by_name(tools, "gmail_search").run(query="from:boss", max_results=5)
    assert out["messages"] == [{"id": "m1", "threadId": "t1"}]
    assert "q=from%3Aboss" in transport.calls[0]["url"]


def test_gmail_read_thread(home_env, monkeypatch):
    body = base64.urlsafe_b64encode(b"hello world").decode()
    tools, _ = _gmail_tools(home_env, monkeypatch, [
        ("GET", "/users/me/threads/t1", 200,
         {"id": "t1", "messages": [{
             "id": "m1", "threadId": "t1", "snippet": "hi",
             "payload": {
                 "headers": [
                     {"name": "From", "value": "a@x.com"},
                     {"name": "Subject", "value": "Hi"}],
                 "parts": [{"mimeType": "text/plain",
                             "body": {"data": body}}]}}]})])
    out = by_name(tools, "gmail_read_thread").run(thread_id="t1")
    assert out["messages"][0]["body"] == "hello world"
    assert out["messages"][0]["subject"] == "Hi"


def test_gmail_send_and_draft(home_env, monkeypatch):
    tools, _ = _gmail_tools(home_env, monkeypatch, [
        ("POST", "/users/me/messages/send", 200,
         {"id": "sent1", "threadId": "th1"}),
        ("POST", "/users/me/drafts", 200,
         {"id": "d1", "message": {"id": "m9"}})])
    out = by_name(tools, "gmail_send").run(
        to="b@x.com", subject="s", body="b")
    assert out["message_id"] == "sent1"
    out = by_name(tools, "gmail_create_draft").run(
        to="b@x.com", subject="s", body="b")
    assert out["draft_id"] == "d1"
    assert by_name(tools, "gmail_send").risk == "high"
    assert by_name(tools, "gmail_create_draft").risk == "medium"


def test_gmail_unauthorized(home_env, monkeypatch):
    tools, _ = _gmail_tools(home_env, monkeypatch, [
        ("GET", "/users/me/messages", 401, {"error": "invalid"})])
    out = by_name(tools, "gmail_search").run(query="x")
    assert out.startswith("ERROR:")
    assert "test-token-abc" not in out


def test_gmail_missing_token(home_env):
    from agentkai.skills_bundle.gmail.tools import get_tools
    tools = get_tools({"home": home_env})
    out = by_name(tools, "gmail_search").run(query="x")
    assert out.startswith("ERROR:")
    assert "GMAIL_TOKEN" in out
    assert "SKILL.md" in out


def test_gmail_token_from_file(home_env, monkeypatch):
    (home_env / "gmail_token.json").write_text(
        json.dumps({"access_token": "file-token"}))
    from agentkai.skills_bundle.gmail.tools import get_tools
    t = FakeTransport([("GET", "/users/me/messages", 200, {"messages": []})])
    tools = get_tools({"home": home_env, "transport": t})
    by_name(tools, "gmail_search").run(query="x")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer file-token"


# ---- google calendar ---------------------------------------------------------

def _gcal_tools(home_env, monkeypatch, routes):
    monkeypatch.setenv("GCAL_TOKEN", "test-token-abc")
    from agentkai.skills_bundle.google_calendar.tools import get_tools
    return get_tools({"home": home_env, "transport": FakeTransport(routes)})


def test_calendar_list(home_env, monkeypatch):
    tools = _gcal_tools(home_env, monkeypatch, [
        ("GET", "/calendars/primary/events", 200,
         {"items": [{"id": "e1", "summary": "Standup",
                     "start": {"dateTime": "2026-10-01T09:00:00+05:00"},
                     "end": {"dateTime": "2026-10-01T09:30:00+05:00"}}]})])
    out = by_name(tools, "calendar_list").run()
    assert out["events"][0]["summary"] == "Standup"
    assert out["events"][0]["start"] == "2026-10-01T09:00:00+05:00"


def test_calendar_create(home_env, monkeypatch):
    t = FakeTransport([("POST", "/calendars/primary/events", 200,
                        {"id": "e9", "summary": "Lunch"})])
    monkeypatch.setenv("GCAL_TOKEN", "x")
    from agentkai.skills_bundle.google_calendar.tools import get_tools
    tools = get_tools({"home": home_env, "transport": t})
    out = by_name(tools, "calendar_create").run(
        summary="Lunch", start="2026-10-01T13:00:00+05:00",
        end="2026-10-01T14:00:00+05:00", attendees=["a@x.com"])
    assert out["event"]["id"] == "e9"
    sent = json.loads(t.calls[0]["body"])
    assert sent["attendees"] == [{"email": "a@x.com"}]
    assert by_name(tools, "calendar_create").risk == "medium"
    assert by_name(tools, "calendar_delete").risk == "high"


def test_calendar_delete_and_update(home_env, monkeypatch):
    tools = _gcal_tools(home_env, monkeypatch, [
        ("DELETE", "/calendars/primary/events/e1", 204, b""),
        ("PATCH", "/calendars/primary/events/e2", 200,
         {"id": "e2", "summary": "New title"})])
    assert by_name(tools, "calendar_delete").run(event_id="e1") == {
        "ok": True, "deleted": "e1"}
    out = by_name(tools, "calendar_update").run(
        event_id="e2", summary="New title")
    assert out["event"]["summary"] == "New title"
    assert by_name(tools, "calendar_update").run(
        event_id="e2").startswith("ERROR: nothing to update")


def test_calendar_missing_token(home_env):
    from agentkai.skills_bundle.google_calendar.tools import get_tools
    out = by_name(get_tools({"home": home_env}),
                  "calendar_list").run()
    assert "GCAL_TOKEN" in out


# ---- github ------------------------------------------------------------------

def _gh_tools(home_env, monkeypatch, routes):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-abc")
    from agentkai.skills_bundle.github.tools import get_tools
    return get_tools({"home": home_env, "transport": FakeTransport(routes)})


def test_github_list_repos(home_env, monkeypatch):
    tools = _gh_tools(home_env, monkeypatch, [
        ("GET", "/user/repos", 200,
         [{"full_name": "me/agentkai", "description": "d",
           "private": False, "updated_at": "2026-09-29"}])])
    out = by_name(tools, "github_list_repos").run()
    assert out[0]["full_name"] == "me/agentkai"


def test_github_issues_and_prs(home_env, monkeypatch):
    tools = _gh_tools(home_env, monkeypatch, [
        ("GET", "/repos/o/r/issues", 200,
         [{"number": 1, "title": "bug", "state": "open",
           "user": {"login": "me"}, "labels": [],
           "html_url": "https://x/1"}]),
        ("GET", "/repos/o/r/pulls", 200,
         [{"number": 2, "title": "fix", "state": "open",
           "user": {"login": "me"}, "head": {"ref": "f"},
           "base": {"ref": "main"}, "merged_at": None,
           "html_url": "https://x/2"}]),
    ])
    issues = by_name(tools, "github_list_issues").run(owner="o", repo="r")
    assert issues[0]["number"] == 1
    prs = by_name(tools, "github_list_prs").run(owner="o", repo="r")
    assert prs[0]["merged"] is False
    assert by_name(tools, "github_list_issues").run(
        owner="o", repo="r", state="bogus").startswith("ERROR:")


def test_github_write_ops_high_risk(home_env, monkeypatch):
    t = FakeTransport([
        ("POST", "/repos/o/r/issues/5/comments", 201,
         {"id": 77, "html_url": "https://x/c77"}),
        ("POST", "/repos/o/r/issues", 201,
         {"number": 5, "html_url": "https://x/5"}),
        ("POST", "/repos/o/r/pulls", 201,
         {"number": 6, "html_url": "https://x/6"}),
    ])
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    from agentkai.skills_bundle.github.tools import get_tools
    tools = get_tools({"home": home_env, "transport": t})
    assert by_name(tools, "github_create_issue").run(
        owner="o", repo="r", title="t")["number"] == 5
    assert by_name(tools, "github_open_pr").run(
        owner="o", repo="r", title="t", head="f", base="main")["number"] == 6
    assert by_name(tools, "github_comment_issue").run(
        owner="o", repo="r", number=5, body="hi")["comment_id"] == 77
    for n in ("github_create_issue", "github_open_pr", "github_comment_issue"):
        assert by_name(tools, n).risk == "high"
    assert by_name(tools, "github_create_issue").run(
        owner="o", repo="r", title="  ").startswith("ERROR:")


def test_github_workflow_runs(home_env, monkeypatch):
    tools = _gh_tools(home_env, monkeypatch, [
        ("GET", "/repos/o/r/actions/runs", 200,
         {"workflow_runs": [{"id": 1, "name": "CI", "status": "completed",
                             "conclusion": "success", "head_branch": "main",
                             "event": "push", "created_at": "2026-09-29",
                             "html_url": "https://x"}]})])
    out = by_name(tools, "github_list_workflow_runs").run(owner="o", repo="r")
    assert out[0]["conclusion"] == "success"


def test_github_api_error(home_env, monkeypatch):
    tools = _gh_tools(home_env, monkeypatch, [
        ("GET", "/repos/o/r/issues", 404, {"message": "Not Found"})])
    out = by_name(tools, "github_list_issues").run(owner="o", repo="r")
    assert out.startswith("ERROR:")
    assert "Not Found" in out
    assert "test-token-abc" not in out


def test_github_missing_token(home_env):
    from agentkai.skills_bundle.github.tools import get_tools
    out = by_name(get_tools({"home": home_env}),
                  "github_list_repos").run()
    assert "GITHUB_TOKEN" in out


# ---- spotify -----------------------------------------------------------------

def _sp_tools(home_env, monkeypatch, routes):
    monkeypatch.setenv("SPOTIFY_TOKEN", "test-token-abc")
    from agentkai.skills_bundle.spotify.tools import get_tools
    return get_tools({"home": home_env, "transport": FakeTransport(routes)})


def test_spotify_search(home_env, monkeypatch):
    tools = _sp_tools(home_env, monkeypatch, [
        ("GET", "/search", 200,
         {"tracks": {"items": [{"uri": "spotify:track:1", "name": "Song",
                                "external_urls": {
                                    "spotify": "https://open/1"}}]},
          "albums": {"items": []}, "artists": {"items": []},
          "playlists": {"items": []}})])
    out = by_name(tools, "spotify_search").run(query="song")
    assert out["tracks"][0]["uri"] == "spotify:track:1"


def test_spotify_now_playing(home_env, monkeypatch):
    tools = _sp_tools(home_env, monkeypatch, [
        ("GET", "/me/player/currently-playing", 200,
         {"is_playing": True, "progress_ms": 1234,
          "device": {"name": "phone"},
          "item": {"uri": "spotify:track:1", "name": "Song",
                   "artists": [{"name": "A"}], "album": {"name": "Al"},
                   "duration_ms": 200000,
                   "external_urls": {"spotify": "https://open/1"}}})])
    out = by_name(tools, "spotify_now_playing").run()
    assert out["is_playing"] is True
    assert out["track"]["artists"] == ["A"]


def test_spotify_now_playing_nothing(home_env, monkeypatch):
    tools = _sp_tools(home_env, monkeypatch, [
        ("GET", "/me/player/currently-playing", 204, b"")])
    out = by_name(tools, "spotify_now_playing").run()
    assert out["is_playing"] is False


def test_spotify_playback_controls(home_env, monkeypatch):
    t = FakeTransport([
        ("PUT", "/me/player/play", 204, b""),
        ("PUT", "/me/player/pause", 204, b""),
        ("POST", "/me/player/queue", 204, b""),
    ])
    monkeypatch.setenv("SPOTIFY_TOKEN", "x")
    from agentkai.skills_bundle.spotify.tools import get_tools
    tools = get_tools({"home": home_env, "transport": t})
    assert by_name(tools, "spotify_play").run(
        context_uri="spotify:album:1") == {"ok": True}
    assert by_name(tools, "spotify_pause").run() == {"ok": True}
    assert by_name(tools, "spotify_queue").run(
        uri="spotify:track:2")["queued"] == "spotify:track:2"
    for n in ("spotify_play", "spotify_pause", "spotify_queue"):
        assert by_name(tools, n).risk == "medium"
    assert "uri=spotify%3Atrack%3A2" in t.calls[2]["url"]


def test_spotify_missing_token(home_env):
    from agentkai.skills_bundle.spotify.tools import get_tools
    out = by_name(get_tools({"home": home_env}),
                  "spotify_search").run(query="x")
    assert "SPOTIFY_TOKEN" in out


# ---- supply-chain hardening (0.5.2) ---------------------------------------

def test_install_records_content_hash(home_env, tmp_path):
    src = _make_skill_dir(tmp_path)
    (src / "tools.py").write_text(
        "def get_tools(config):\n    return []\n")
    dest = install_skill(str(src))
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["content_sha256"]
    assert SkillLoader().load("myskill").name == "myskill"


def test_load_refuses_drifted_user_skill(home_env, tmp_path):
    src = _make_skill_dir(tmp_path)
    sentinel = tmp_path / "pwned.txt"
    (src / "tools.py").write_text(
        f"open(r{str(sentinel)!r}, 'w').write('x')\n"
        "def get_tools(config):\n    return []\n")
    dest = install_skill(str(src))
    assert not sentinel.exists()
    (dest / "tools.py").write_text(
        f"open(r{str(sentinel)!r}, 'w').write('tampered')\n"
        "def get_tools(config):\n    return []  # tampered\n")
    with pytest.raises(SkillError, match="drift"):
        SkillLoader().load("myskill")
    assert not sentinel.exists()


def test_remote_install_requires_trust(home_env, monkeypatch):
    import subprocess as _sp
    calls = []

    def fake_run(*a, **k):  # pragma: no cover - must not run without trust
        calls.append(a)
        return _sp.CompletedProcess(a[0], 1, "", "nope")

    monkeypatch.setattr("agentkai.skills.subprocess.run", fake_run)
    with pytest.raises(SkillError, match="trust"):
        install_skill("owner/repo")
    assert calls == []
    with pytest.raises(SkillError, match="git clone failed"):
        install_skill("owner/repo", trust_remote=True)
    assert calls, "clone should be attempted with trust"


def test_install_pin_mismatch_fails(home_env, tmp_path, monkeypatch):
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(
        "agentkai.skills._git_head_sha", lambda dest: "a" * 40)
    (src / "tools.py").write_text(
        "def get_tools(config):\n    return []\n")
    # Local installs ignore pin; exercise pin via git path by faking clone.
    import shutil as _sh

    def fake_clone(cmd, capture_output, text, timeout):
        dest = tmp_path / "unused"  # not used; we copy src below instead
        return type("P", (), {"returncode": 0, "stderr": ""})()

    # Drive the git branch: local.is_dir() is False for the URL source.
    orig_run = __import__("subprocess").run

    def fake_run(cmd, capture_output=True, text=True, timeout=120):
        if cmd[:2] == ["git", "clone"]:
            _sh.copytree(src, cmd[-1])
            return type("P", (), {"returncode": 0, "stderr": ""})()
        return orig_run(cmd, capture_output=capture_output, text=text,
                        timeout=timeout)

    monkeypatch.setattr("agentkai.skills.subprocess.run", fake_run)
    monkeypatch.setattr(
        "agentkai.skills._git_head_sha", lambda dest: "b" * 40)
    with pytest.raises(SkillError, match="pin"):
        install_skill("owner/repo", trust_remote=True, pin="a" * 40)
    assert not (home_env / "skills" / "myskill").exists()


# ---- manifest fail-closed (0.5.3) -------------------------------------------

def _install_marker_skill(home_env, tmp_path):
    """Install a skill whose tools.py drops a sentinel file on import."""
    src = _make_skill_dir(tmp_path)
    sentinel = tmp_path / "pwned.txt"
    (src / "tools.py").write_text(
        f"open(r{str(sentinel)!r}, 'w').write('x')\n"
        "def get_tools(config):\n    return []\n")
    dest = install_skill(str(src))
    assert not sentinel.exists()
    return dest, sentinel


def test_load_refuses_corrupt_manifest(home_env, tmp_path):
    dest, sentinel = _install_marker_skill(home_env, tmp_path)
    (dest / "manifest.json").write_text("{ not json !!")
    with pytest.raises(SkillError, match="manifest is unreadable"):
        SkillLoader().load("myskill")
    assert not sentinel.exists()


def test_load_refuses_manifest_without_hash(home_env, tmp_path):
    dest, sentinel = _install_marker_skill(home_env, tmp_path)
    manifest = json.loads((dest / "manifest.json").read_text())
    del manifest["content_sha256"]  # shape stamped by agentkai <= 0.5.1
    (dest / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SkillError, match="no content_sha256"):
        SkillLoader().load("myskill")
    assert not sentinel.exists()


def test_load_refuses_non_object_manifest(home_env, tmp_path):
    dest, sentinel = _install_marker_skill(home_env, tmp_path)
    (dest / "manifest.json").write_text('["not", "an", "object"]')
    with pytest.raises(SkillError, match="not a JSON object"):
        SkillLoader().load("myskill")
    assert not sentinel.exists()


def test_load_allows_skill_without_manifest(home_env, tmp_path):
    # Manually copied dirs (no manifest at all) keep loading as before.
    dest, sentinel = _install_marker_skill(home_env, tmp_path)
    (dest / "manifest.json").unlink()
    assert SkillLoader().load("myskill").name == "myskill"
    assert sentinel.exists()


def test_list_skills_flags_unverifiable_manifest(home_env, tmp_path):
    dest, _sentinel = _install_marker_skill(home_env, tmp_path)
    (dest / "manifest.json").write_text("{ not json !!")
    rows = {r["name"]: r for r in list_skills()}
    assert "error" in rows["myskill"]
    assert "manifest" in rows["myskill"]["error"]
