"""Paired devices: pairing flow, push notifications, SMS/contacts/calendar via the companion app.

AgentKai never talks to a phone directly — it talks to the AgentKai companion
app over plain HTTPS. The app is the thing that can send SMS, read contacts,
and read the calendar on the device; without it installed, device tools
report an honest error instead of pretending.

The HTTP contract the companion app must implement is documented in
``src/agentkai/devices/README.md``. The pairing/credential side lives here:

- ``agentkai devices pair`` prints a short pairing code (valid 10 minutes).
- The app claims the code: ``POST {dashboard}/api/devices/claim``
  with ``{"code": "...", "device": {...}}`` and gets back a bearer token.
- Every later call from the agent to the app carries that token; the token
  is stored here only as a SHA-256 hash.

Because the dashboard binds 127.0.0.1 only, a phone on the LAN cannot reach
the claim endpoint directly. Two honest options, both documented in
README.md: (1) complete pairing on the phone's browser pointed at the
dashboard via an SSH/HTTPS tunnel you control, or (2) a relay you run
yourself. AgentKai ships no cloud relay — your data stays yours.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import string
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..tools import Tool

DEFAULT_DB = Path("~/.agentkai/devices.db").expanduser()

PAIRING_TTL_SECONDS = 10 * 60
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


@dataclass
class Device:
    id: int
    name: str
    device_type: str
    paired_at: float
    last_seen: float | None
    api_base: str  # the companion app's own HTTPS base URL, "" if unknown
    capabilities: dict  # e.g. {"sms": True, "contacts": True, ...}


class DeviceError(Exception):
    """Pairing or device-communication failure."""


def _utcnow() -> float:
    return time.time()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _http_post(url: str, payload: dict, token: str, timeout: int = 20) -> dict:
    """Default HTTPS POST to the companion app. Injected/replaced in tests."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "agentkai/0.1 (+local agent)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(1 << 20)  # 1 MiB cap
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return {"raw": data.decode("utf-8", errors="replace")[:2000]}


def _http_get(url: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "agentkai/0.1 (+local agent)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(1 << 20)
    parsed = json.loads(data.decode("utf-8", errors="replace"))
    return parsed if isinstance(parsed, dict) else {"items": parsed}


class DeviceManager:
    """Pairing, credentials, and companion-app calls for user devices."""

    def __init__(self, db_path: str | Path | None = None,
                 http_post=_http_post, http_get=_http_get):
        self.db_path = Path(db_path).expanduser() if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._post = http_post
        self._get = http_get
        self._init_schema()

    # ---- storage --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    device_type TEXT NOT NULL DEFAULT 'phone',
                    paired_at REAL NOT NULL,
                    last_seen REAL,
                    api_base TEXT NOT NULL DEFAULT '',
                    capabilities TEXT NOT NULL DEFAULT '{}',
                    token_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivered_at REAL,
                    error TEXT
                );
            """)

    # ---- pairing --------------------------------------------------------

    def generate_pairing_code(self, name: str = "phone") -> dict:
        """Create a short pairing code for a new device (valid 10 min)."""
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pairing_codes (code, name, created_at, "
                "expires_at, status) VALUES (?, ?, ?, ?, 'pending')",
                (code, name, now, now + PAIRING_TTL_SECONDS),
            )
        return {"code": code, "name": name,
                "expires_in_seconds": PAIRING_TTL_SECONDS}

    def claim_pairing(self, code: str, device_info: dict) -> dict:
        """Complete pairing from the companion app side. Returns a token.

        ``device_info`` may carry ``name``, ``device_type`` (phone/laptop),
        ``api_base`` (the app's own HTTPS base URL), and ``capabilities``
        ({"sms": bool, "contacts": bool, "calendar": bool, "push": bool}).
        """
        code = code.strip().upper()
        now = _utcnow()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise DeviceError("unknown pairing code")
            if row["status"] != "pending":
                raise DeviceError("pairing code already used")
            if row["expires_at"] < now:
                raise DeviceError("pairing code expired")
            token = secrets.token_urlsafe(32)
            cur = conn.execute(
                "INSERT INTO devices (name, device_type, paired_at, "
                "last_seen, api_base, capabilities, token_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (device_info.get("name", row["name"]),
                 device_info.get("device_type", "phone"),
                 now, now,
                 device_info.get("api_base", ""),
                 json.dumps(device_info.get("capabilities", {})),
                 _hash_token(token)),
            )
            conn.execute(
                "UPDATE pairing_codes SET status = 'claimed' WHERE code = ?",
                (code,))
            device_id = cur.lastrowid
        return {"device_id": device_id, "token": token,
                "warning": "store this token in the app; it is shown once"}

    # ---- device management ----------------------------------------------

    def _row_to_device(self, row: sqlite3.Row) -> Device:
        try:
            caps = json.loads(row["capabilities"] or "{}")
        except ValueError:
            caps = {}
        return Device(
            id=row["id"], name=row["name"], device_type=row["device_type"],
            paired_at=row["paired_at"], last_seen=row["last_seen"],
            api_base=row["api_base"] or "",
            capabilities=caps if isinstance(caps, dict) else {},
        )

    def list_devices(self) -> list[Device]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY paired_at").fetchall()
        return [self._row_to_device(r) for r in rows]

    def get_device(self, device_id: int) -> Device | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return self._row_to_device(row) if row else None

    def resolve_device(self, ref: str | int | None = None) -> Device:
        """Resolve a device by id, by name, or default to the first paired."""
        devices = self.list_devices()
        if not devices:
            raise DeviceError("no paired devices — run `agentkai devices pair`")
        if ref is None:
            return devices[0]
        for d in devices:
            if str(d.id) == str(ref) or d.name.lower() == str(ref).lower():
                return d
        raise DeviceError(f"no paired device matches {ref!r}")

    def verify_token(self, token: str) -> Device | None:
        """Verify a bearer token presented by the companion app."""
        digest = _hash_token(token)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE token_hash = ?",
                (digest,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?",
                         (_utcnow(), row["id"]))
        return self._row_to_device(row)

    def revoke(self, device_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id = ?",
                               (device_id,))
        return cur.rowcount > 0

    # ---- notifications ---------------------------------------------------

    def enqueue_notification(self, title: str, body: str,
                             device_id: int | None = None) -> int:
        """Queue a push notification for a device (or all paired devices)."""
        if device_id is not None and self.get_device(device_id) is None:
            raise DeviceError(f"no paired device with id {device_id}")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notifications (device_id, title, body, "
                "created_at) VALUES (?, ?, ?, ?)",
                (device_id, title, body, _utcnow()))
            return cur.lastrowid

    def pending_notifications(self, device: Device) -> list[dict]:
        """Notifications the companion app has not yet fetched (poll API)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, body, created_at FROM notifications "
                "WHERE delivered = 0 AND (device_id IS NULL "
                "OR device_id = ?) ORDER BY created_at",
                (device.id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_notifications_delivered(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE notifications SET delivered = 1, delivered_at = ? "
                "WHERE id = ?",
                [(_utcnow(), i) for i in ids])

    # ---- companion-app calls ---------------------------------------------

    def _call_app(self, device: Device, path: str, payload: dict | None,
                  capability: str, token: str | None = None) -> dict:
        if not device.api_base:
            return {"ERROR": (
                f"device {device.name!r} has no companion-app endpoint "
                f"registered (api_base empty). The AgentKai companion app "
                f"must be installed and paired to use '{capability}'. "
                f"See src/agentkai/devices/README.md.")}
        if not device.capabilities.get(capability, False):
            return {"ERROR": (
                f"device {device.name!r} does not advertise the "
                f"'{capability}' capability.")}
        # The agent authenticates to the app with the same bearer token the
        # app received at pairing. We only store its hash, so the token must
        # be supplied here — the dashboard holds it in-memory per session.
        # For CLI use, pass --token explicitly; it is never logged.
        if not token:
            return {"ERROR": (
                "a device token is required to call the companion app; "
                "it was shown once at pairing time and is not stored "
                "server-side.")}
        url = device.api_base.rstrip("/") + path
        try:
            if payload is None:
                return self._get(url, token)
            return self._post(url, payload, token)
        except Exception as exc:  # noqa: BLE001 - surfaced as tool output
            return {"ERROR": f"companion app call failed: {exc}"}

    def send_sms(self, to: str, body: str,
                 device: Device | None = None, token: str | None = None) -> dict:
        """Ask the companion app to send an SMS. High risk: goes via gate."""
        device = device or self.resolve_device()
        if not to.strip() or not body.strip():
            return "ERROR: 'to' and 'body' must both be non-empty"
        return self._call_app(device, "/agentkai/v1/sms",
                              {"to": to, "body": body}, "sms", token)

    def read_contacts(self, query: str = "",
                      device: Device | None = None,
                      token: str | None = None) -> dict:
        """Read contacts from the companion app (read-only)."""
        device = device or self.resolve_device()
        q = "?q=" + query if query else ""
        return self._call_app(device, f"/agentkai/v1/contacts{q}",
                              None, "contacts", token)

    def read_calendar(self, days: int = 7,
                      device: Device | None = None,
                      token: str | None = None) -> dict:
        """Read upcoming calendar events from the companion app (read-only)."""
        device = device or self.resolve_device()
        days = max(1, min(int(days), 60))
        return self._call_app(device, f"/agentkai/v1/calendar?days={days}",
                              None, "calendar", token)


# ---- agent tools --------------------------------------------------------

def device_tools(manager: DeviceManager | None = None) -> list[Tool]:
    """Tools the agent uses to reach the user's paired devices."""
    mgr = manager or DeviceManager()

    def _notify(title: str, body: str, device: str = "") -> dict:
        try:
            d = mgr.resolve_device(device or None)
        except DeviceError as exc:
            return f"ERROR: {exc}"
        nid = mgr.enqueue_notification(title, body, d.id)
        return {"ok": True, "notification_id": nid, "device": d.name,
                "note": "queued; the companion app delivers it on next poll"}

    def _send_sms(to: str, body: str, device: str = "",
                  token: str = "") -> dict:
        try:
            d = mgr.resolve_device(device or None)
        except DeviceError as exc:
            return f"ERROR: {exc}"
        return mgr.send_sms(to, body, d, token or None)

    def _contacts(query: str = "", device: str = "",
                  token: str = "") -> dict:
        try:
            d = mgr.resolve_device(device or None)
        except DeviceError as exc:
            return f"ERROR: {exc}"
        return mgr.read_contacts(query, d, token or None)

    def _calendar(days: int = 7, device: str = "",
                  token: str = "") -> dict:
        try:
            d = mgr.resolve_device(device or None)
        except DeviceError as exc:
            return f"ERROR: {exc}"
        return mgr.read_calendar(days, d, token or None)

    return [
        Tool(
            name="device_notify",
            description=(
                "Queue a push notification for the user's paired phone/"
                "laptop. The companion app delivers it on its next poll."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "device": {"type": "string",
                               "description": "device id or name; default: first paired"},
                },
                "required": ["title", "body"],
            },
            risk="medium",
            func=_notify,
        ),
        Tool(
            name="send_sms",
            description=(
                "Send an SMS through the user's paired phone via the "
                "companion app. Requires the app; high risk, needs approval."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string",
                           "description": "recipient phone number"},
                    "body": {"type": "string"},
                    "device": {"type": "string"},
                    "token": {"type": "string",
                              "description": "device token from pairing time"},
                },
                "required": ["to", "body"],
            },
            risk="high",
            func=_send_sms,
        ),
        Tool(
            name="device_contacts",
            description="Search contacts on the user's paired device (read-only, via companion app).",
            json_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "device": {"type": "string"},
                    "token": {"type": "string"},
                },
            },
            risk="low",
            func=_contacts,
        ),
        Tool(
            name="device_calendar",
            description="Read upcoming calendar events from the user's paired device (read-only, via companion app).",
            json_schema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer"},
                    "device": {"type": "string"},
                    "token": {"type": "string"},
                },
            },
            risk="low",
            func=_calendar,
        ),
    ]
