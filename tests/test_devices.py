"""Tests for devices: pairing flow, gating, companion-app contract (fake HTTP)."""
import pytest

from agentkai.devices import (DeviceError, DeviceManager, _hash_token,
                              device_tools)
from agentkai.tools import ApprovalRequired, PermissionDenied


@pytest.fixture()
def mgr(tmp_path):
    return DeviceManager(db_path=tmp_path / "devices.db")


def test_pairing_and_claim(mgr):
    info = mgr.generate_pairing_code("pixel")
    assert len(info["code"]) == 6
    claimed = mgr.claim_pairing(info["code"], {
        "name": "pixel-8", "device_type": "phone",
        "api_base": "https://app.local",
        "capabilities": {"sms": True, "contacts": True,
                         "calendar": False, "push": True},
    })
    assert "token" in claimed and claimed["device_id"] == 1
    devices = mgr.list_devices()
    assert len(devices) == 1
    assert devices[0].capabilities["sms"] is True
    # token verifies, hash not equal to raw token
    verified = mgr.verify_token(claimed["token"])
    assert verified is not None and verified.name == "pixel-8"
    assert mgr.verify_token("bogus") is None


def test_pairing_code_single_use_and_case_insensitive(mgr):
    info = mgr.generate_pairing_code()
    mgr.claim_pairing(info["code"].lower(), {})
    with pytest.raises(DeviceError):
        mgr.claim_pairing(info["code"], {})
    with pytest.raises(DeviceError):
        mgr.claim_pairing("ZZZZZZ", {})


def test_pairing_code_expired(mgr, monkeypatch):
    import agentkai.devices as dev
    real_now = dev._utcnow
    info = mgr.generate_pairing_code()
    monkeypatch.setattr(dev, "_utcnow",
                        lambda: real_now() + dev.PAIRING_TTL_SECONDS + 1)
    with pytest.raises(DeviceError, match="expired"):
        mgr.claim_pairing(info["code"], {})


def test_revoke_kills_token(mgr):
    info = mgr.generate_pairing_code()
    claimed = mgr.claim_pairing(info["code"], {})
    assert mgr.revoke(claimed["device_id"]) is True
    assert mgr.verify_token(claimed["token"]) is None
    assert mgr.revoke(999) is False


def _paired_with_app(mgr):
    posts, gets = [], []

    def fake_post(url, payload, token, timeout=20):
        posts.append((url, payload, token))
        return {"message_id": "m1"}

    def fake_get(url, token, timeout=20):
        gets.append((url, token))
        return {"contacts": [{"name": "Bilal"}]}

    m2 = DeviceManager(db_path=mgr.db_path, http_post=fake_post,
                       http_get=fake_get)
    info = m2.generate_pairing_code()
    claimed = m2.claim_pairing(info["code"], {
        "api_base": "https://app.local:8443",
        "capabilities": {"sms": True, "contacts": True,
                         "calendar": True, "push": True},
    })
    device = m2.list_devices()[0]
    return m2, device, claimed["token"], posts, gets


def test_send_sms_uses_app_contract(mgr):
    m2, device, token, posts, _ = _paired_with_app(mgr)
    out = m2.send_sms("+921234567890", "hello", device, token)
    assert out == {"message_id": "m1"}
    url, payload, tok = posts[0]
    assert url == "https://app.local:8443/agentkai/v1/sms"
    assert payload == {"to": "+921234567890", "body": "hello"}
    assert tok == token  # bearer token, never logged


def test_send_sms_without_app_is_honest_error(mgr):
    info = mgr.generate_pairing_code()
    mgr.claim_pairing(info["code"], {})  # no api_base, no capabilities
    device = mgr.list_devices()[0]
    out = mgr.send_sms("+921", "hi", device, token="t")
    assert out["ERROR"].startswith("device") and "companion app" in out["ERROR"]


def test_send_sms_missing_token_is_honest_error(mgr):
    m2, device, _token, _p, _g = _paired_with_app(mgr)
    out = m2.send_sms("+921", "hi", device, token=None)
    assert "token is required" in out["ERROR"]


def test_contacts_and_calendar_read_paths(mgr):
    m2, device, token, _p, gets = _paired_with_app(mgr)
    out = m2.read_contacts("bilal", device, token)
    assert out["contacts"][0]["name"] == "Bilal"
    assert gets[0][0] == "https://app.local:8443/agentkai/v1/contacts?q=bilal"
    out2 = m2.read_calendar(3, device, token)
    assert gets[1][0].endswith("/agentkai/v1/calendar?days=3")


def test_notifications_queue_and_poll(mgr):
    m2, device, _token, _p, _g = _paired_with_app(mgr)
    nid = m2.enqueue_notification("hi", "body", device.id)
    pending = m2.pending_notifications(device)
    assert len(pending) == 1 and pending[0]["id"] == nid
    m2.mark_notifications_delivered([nid])
    assert m2.pending_notifications(device) == []


def test_sms_tool_gating(mgr):
    m2, device, token, _p, _g = _paired_with_app(mgr)
    tools = {t.name: t for t in device_tools(m2)}
    sms = tools["send_sms"]
    assert sms.risk == "high"

    def deny(action):
        assert action["tool"] == "send_sms"
        assert action["risk"] == "high"
        return "deny"

    def ask(action):
        return "ask"

    with pytest.raises(PermissionDenied):
        sms.run(_gate=deny, to="+921", body="hi")
    with pytest.raises(ApprovalRequired):
        sms.run(_gate=ask, to="+921", body="hi")
    out = sms.run(_gate=lambda a: "allow", to="+921", body="hi",
                  device=str(device.id), token=token)
    assert out == {"message_id": "m1"}


def test_notify_tool_queues(mgr):
    tools = {t.name: t for t in device_tools(mgr)}
    out = tools["device_notify"].run(title="t", body="b")
    assert out.startswith("ERROR:")  # no paired devices -> honest error
    info = mgr.generate_pairing_code()
    mgr.claim_pairing(info["code"], {})
    out = tools["device_notify"].run(title="t", body="b")
    assert out["ok"] is True


def test_hash_token_deterministic():
    assert _hash_token("x") == _hash_token("x")
    assert _hash_token("x") != _hash_token("y")
