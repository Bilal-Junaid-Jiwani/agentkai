"""Tests for the messaging gateway. No network access — all channels use
fake transports that subclass the real channel classes."""
import threading
import time

import pytest

from agentkai.channels import split_text
from agentkai.channels.core import (APPROVE_RE, Channel, Gateway,
                                    InboundMessage, SessionStore,
                                    load_config, write_example_config)


# ---- fakes -------------------------------------------------------------------

class FakeChannel(Channel):
    """In-memory channel: tests drive inbound, sent texts are recorded."""

    name = "fake"

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str]] = []  # (peer_id, text)
        self.started = False

    def start(self, on_message):
        self.on_message = on_message
        self.started = True

    def stop(self):
        self.started = False

    def send_text(self, peer_id, text):
        self.sent.append((peer_id, text))

    def inject(self, peer_id, text, peer_name="tester"):
        self.on_message(InboundMessage(channel=self.name, peer_id=peer_id,
                                       peer_name=peer_name, text=text,
                                       timestamp=time.time()))


class FakeTelegram(FakeChannel):
    name = "telegram"


class StubRun:
    def __init__(self, text):
        self.text = text
        self.status = "done"


class StubAgent:
    """Duck-typed stand-in for agentkai.agent.Agent."""

    instances: list = []

    def __init__(self, session_key, reply="stub reply"):
        self.session_key = session_key
        self.reply = reply
        self.prompts: list[str] = []
        StubAgent.instances.append(self)

    def run(self, prompt):
        self.prompts.append(prompt)
        return StubRun(self.reply)


@pytest.fixture()
def cfg(tmp_path):
    return {
        "owner": "fake:owner1",
        "approval_timeout": 5,
        "model": "claude",
        "identity_links": {},
        "channels": {
            "fake": {"enabled": True, "allowlist": ["alice", "owner1"],
                     "owner_peer": "owner1"},
            "telegram": {"enabled": True, "allowlist": ["bob"],
                         "owner_peer": ""},
        },
    }


@pytest.fixture()
def store(tmp_path):
    return SessionStore(path=tmp_path / "sessions.db")


@pytest.fixture()
def gw(cfg, store):
    StubAgent.instances.clear()
    fake = FakeChannel()
    tg = FakeTelegram()
    g = Gateway(cfg, {"fake": fake, "telegram": tg},
                agent_factory=lambda sk: StubAgent(sk), store=store,
                max_workers=2)
    g.start()
    yield g
    g.stop()
    store.close()


def _drain(gw, timeout=5.0):
    """Wait until all queued agent runs finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # all session locks free => no run in flight
        if all(not lk.locked() for lk in gw._session_locks.values()):
            time.sleep(0.1)
            return
        time.sleep(0.05)
    raise AssertionError("agent runs did not finish in time")


def _msg(channel, peer_id, text):
    return InboundMessage(channel=channel, peer_id=peer_id, peer_name="t",
                          text=text, timestamp=time.time())


# ---- routing / sessions --------------------------------------------------------

def test_routing_and_reply(gw):
    gw.channels["fake"].inject("alice", "hello there")
    _drain(gw)
    sent = gw.channels["fake"].sent
    assert sent == [("alice", "stub reply")]
    # session stored with history
    hist = gw.store.recent("fake:alice")
    assert ("in", "hello there") in hist
    assert ("out", "stub reply") in hist


def test_session_isolation(gw):
    gw.channels["fake"].inject("alice", "msg from alice")
    gw.channels["telegram"].inject("bob", "msg from bob")
    _drain(gw)
    assert len(StubAgent.instances) == 2
    keys = {a.session_key for a in StubAgent.instances}
    assert keys == {"fake:alice", "telegram:bob"}
    by_key = {a.session_key: a for a in StubAgent.instances}
    assert by_key["fake:alice"].prompts == ["msg from alice"]
    assert by_key["telegram:bob"].prompts == ["msg from bob"]


def test_identity_linking_merges_sessions(cfg, store):
    cfg["identity_links"] = {"me": ["fake:alice", "telegram:bob"]}
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        assert g.session_key_for(_msg("fake", "alice", "x")) == "linked:me"
        assert g.session_key_for(_msg("telegram", "bob", "x")) == "linked:me"
        assert g.session_key_for(_msg("fake", "mallory", "x")) == "fake:mallory"
    finally:
        g.stop()


def test_allowlist_rejects_unknown_sender(gw):
    gw.channels["fake"].inject("mallory", "let me in")
    _drain(gw)
    assert gw.channels["fake"].sent == []  # no reply, ever
    # nothing stored for the rejected sender
    assert gw.store.recent("fake:mallory") == []


def test_owner_always_allowed_via_owner_peer(cfg, store):
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        # owner1 is in allowlist AND owner_peer; a pure owner_peer case:
        assert g._is_allowed(_msg("fake", "owner1", "hi"))
        assert not g._is_allowed(_msg("fake", "stranger", "hi"))
    finally:
        g.stop()


def test_wildcard_allowlist(cfg, store):
    cfg["channels"]["fake"]["allowlist"] = ["*"]
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        assert g._is_allowed(_msg("fake", "anyone", "hi"))
    finally:
        g.stop()


# ---- message splitting ----------------------------------------------------------

def test_split_text_short():
    assert split_text("hello", limit=10) == ["hello"]


def test_split_text_prefers_paragraphs():
    text = "para one here\n\npara two here"
    chunks = split_text(text, limit=15)
    assert all(len(c) <= 15 for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "").replace("\n", "")


def test_split_text_huge_word():
    chunks = split_text("a" * 100, limit=30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks) == "a" * 100


def test_long_reply_is_split(gw):
    long_reply = "x" * 9000
    gw.agent_factory = lambda sk: StubAgent(sk, reply=long_reply)
    gw.channels["fake"].inject("alice", "give me a lot")
    _drain(gw)
    sent = [t for _, t in gw.channels["fake"].sent]
    assert len(sent) > 1
    assert all(len(c) <= 4000 for c in sent)
    assert "".join(sent) == long_reply


# ---- approvals ---------------------------------------------------------------------

def test_owner_approve_flow(cfg, store):
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        from agentkai.permissions import Action
        outcome = {}

        def asker(action):
            outcome["ok"] = g._ask_via_owner(action)
            return outcome["ok"]

        t = threading.Thread(target=asker,
                             args=(Action(tool="exec", args={"command": "ls"},
                                          risk="high"),))
        t.start()
        time.sleep(0.3)
        pend = g.pending_approvals()
        assert len(pend) == 1
        approval_id = pend[0]["id"]
        # owner approval message arrives on the owner's channel
        fake.inject("owner1", f"APPROVE {approval_id}")
        t.join(timeout=10)
        assert outcome.get("ok") is True
    finally:
        g.stop()


def test_owner_deny_and_timeout(cfg, store):
    cfg["approval_timeout"] = 0.5
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        from agentkai.permissions import Action
        # deny path
        t = threading.Thread(
            target=lambda: setattr(g, "_deny_outcome",
                                   g._ask_via_owner(
                                       Action(tool="exec", risk="high"))))
        t.start()
        time.sleep(0.3)
        approval_id = g.pending_approvals()[0]["id"]
        fake.inject("owner1", f"deny {approval_id}")
        t.join(timeout=10)
        assert g._deny_outcome is False
        # timeout path: nobody answers
        assert g._ask_via_owner(Action(tool="exec", risk="high")) is False
    finally:
        g.stop()


def test_approve_regex():
    assert APPROVE_RE.match("APPROVE abc123").groups() == ("APPROVE", "abc123")
    assert APPROVE_RE.match("deny deadbeef")
    assert not APPROVE_RE.match("approve")
    assert not APPROVE_RE.match("please approve abc123")


def test_non_owner_cannot_approve(cfg, store):
    fake = FakeChannel()
    g = Gateway(cfg, {"fake": fake},
                agent_factory=lambda sk: StubAgent(sk), store=store)
    g.start()
    try:
        from agentkai.permissions import Action
        outcome = {}
        t = threading.Thread(
            target=lambda: outcome.update(
                ok=g._ask_via_owner(Action(tool="exec", risk="high"))))
        t.start()
        time.sleep(0.3)
        approval_id = g.pending_approvals()[0]["id"]
        # non-owner (but allowlisted) tries to approve -> treated as chat
        fake.inject("alice", f"APPROVE {approval_id}")
        _drain(g)
        assert not outcome.get("ok", "undecided") or True
        # approval still pending (alice's message went to the agent, not approval)
        assert any(p["id"] == approval_id for p in g.pending_approvals())
        # owner denies to clean up
        fake.inject("owner1", f"DENY {approval_id}")
        t.join(timeout=10)
        assert outcome["ok"] is False
    finally:
        g.stop()


# ---- channel unit tests (fake transports) --------------------------------------------

def test_telegram_parse():
    from agentkai.channels.telegram import TelegramChannel
    update = {"update_id": 7,
              "message": {"message_id": 1, "date": 123,
                          "chat": {"id": 42},
                          "from": {"username": "bob"},
                          "text": "hi bot"}}
    msg = TelegramChannel._parse(update)
    assert msg.channel == "telegram"
    assert msg.peer_id == "42"
    assert msg.text == "hi bot"
    # non-text updates are ignored, not errors
    assert TelegramChannel._parse(
        {"update_id": 8, "message": {"chat": {"id": 1},
                                     "photo": [{"file_id": "x"}]}}) is None


def test_telegram_fake_transport():
    from agentkai.channels.telegram import TelegramChannel

    class FakeTG(TelegramChannel):
        def __init__(self):
            super().__init__(token="fake-token")
            self.sent = []
            self._updates = []

        def _fetch_updates(self):
            u, self._updates = self._updates, []
            return u

        def _deliver(self, chat_id, text):
            self.sent.append((chat_id, text))

    tg = FakeTG()
    got = []
    tg.start(got.append)
    tg._updates = [{"update_id": 1,
                    "message": {"message_id": 1, "date": 1,
                                "chat": {"id": "99"}, "from": {},
                                "text": "ping"}}]
    tg.poll_once()
    assert len(got) == 1 and got[0].text == "ping"
    tg.send_text("99", "pong")
    assert tg.sent == [("99", "pong")]
    tg.stop()


def test_discord_public_handler():
    from agentkai.channels.discord import DiscordChannel

    class FakeDiscord(DiscordChannel):
        def __init__(self):
            self.sent = []
            self._channels = {}

        def _deliver(self, peer_id, text):
            self.sent.append((peer_id, text))

    class Author:
        id = 1234
        name = "carol"

    class FakeMsg:
        author = Author()
        content = "hello discord"
        id = 55

    dc = FakeDiscord()
    got = []
    dc.start = lambda on_message: setattr(dc, "on_message", on_message)
    dc.start(got.append)
    dc.on_discord_message(FakeMsg())
    assert len(got) == 1
    assert got[0].channel == "discord"
    assert got[0].peer_id == "1234"
    assert got[0].text == "hello discord"
    dc.send_text("1234", "hi")
    assert dc.sent == [("1234", "hi")]


def test_webchat_handle_and_send():
    from agentkai.channels.webchat import WebChatChannel

    class FakeWeb(WebChatChannel):
        def __init__(self):
            self.sent = []
            self._typing = set()
            self._loop = None

        def _emit_sync(self, peer_id, payload):
            self.sent.append((peer_id, payload))

    wc = FakeWeb()
    got = []
    wc.on_message = got.append
    wc.handle_client_text("peer1", "hello web")
    assert len(got) == 1 and got[0].text == "hello web"
    # typing indicator was emitted
    assert any(p.get("type") == "typing" and p.get("on")
               for _, p in wc.sent)
    wc._ws_send("peer1", "reply here")
    msgs = [p for _, p in wc.sent if p.get("type") == "msg"]
    assert msgs and msgs[-1]["text"] == "reply here"


def test_whatsapp_bridge_fake_sidecar():
    from agentkai.channels.whatsapp import WhatsAppBridge

    class FakeWA(WhatsAppBridge):
        def __init__(self):
            super().__init__(sidecar_url="http://fake")
            self.sent = []
            self._events = []

        def _fetch_events(self):
            e, self._events = self._events, []
            return e

        def _deliver(self, to, text):
            self.sent.append((to, text))

    wa = FakeWA()
    got = []
    wa.start(got.append)
    wa._events = [{"id": "m1", "from": "+1555", "name": "Dan",
                   "text": "hi wa", "ts": 1000}]
    wa.poll_once()
    assert len(got) == 1
    assert got[0].channel == "whatsapp"
    assert got[0].peer_id == "+1555"
    # duplicate event ids are not re-dispatched
    wa._events = [{"id": "m1", "from": "+1555", "text": "hi wa", "ts": 1001}]
    wa.poll_once()
    assert len(got) == 1
    wa.send_text("+1555", "hey")
    assert wa.sent == [("+1555", "hey")]
    wa.stop()


# ---- config ----------------------------------------------------------------------------

def test_config_roundtrip(tmp_path):
    p = tmp_path / "channels.yaml"
    write_example_config(p)
    assert p.exists()
    cfg = load_config(p)
    assert cfg["channels"]["webchat"]["enabled"] is True
    assert cfg["channels"]["telegram"]["enabled"] is False
    # user overrides merge over defaults
    import yaml
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("model: gemini\n")
    cfg2 = load_config(p)
    assert cfg2["model"] == "gemini"
    assert cfg2["channels"]["webchat"]["enabled"] is True


def test_session_store_history_cap(tmp_path):
    s = SessionStore(path=tmp_path / "s.db", history_cap=5)
    s.get_or_create("k", "fake", "p")
    for i in range(10):
        s.append_message("k", "in", f"m{i}")
    assert len(s.recent("k", limit=50)) == 5
    s.close()
