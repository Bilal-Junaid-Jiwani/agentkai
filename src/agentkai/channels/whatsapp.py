"""WhatsApp channel — EXPERIMENTAL bridge adapter.

There is no native WhatsApp support here, and this module does not pretend
otherwise: WhatsApp has no Bot API for personal accounts, and the web-client
libraries (whatsapp-web.js / Baileys) are Node-only. So agentkai talks to a
small Node **sidecar** over plain HTTP, and the sidecar owns the WhatsApp
Web session (QR pairing happens there):

    sidecar contract (see examples/whatsapp-sidecar/server.js):
      GET  {sidecar_url}/events?since=<unix ts>  -> {"events": [
             {"id": "...", "from": "+15551234567", "text": "...", "ts": 123}]}
      POST {sidecar_url}/send  {"to": "+15551234567", "text": "..."}
             -> {"ok": true}

Setup: run the sidecar (``node server.js``), scan the QR it prints, set
``channels.whatsapp.sidecar_url``, and allowlist your number. Full steps in
channels/README.md. Marked experimental: WhatsApp may rate-limit or ban
numbers that automate the web client — use a spare number.
"""
from __future__ import annotations

import threading
import time

import httpx

from .core import Channel, InboundMessage

MAX_TEXT = 4000


class WhatsAppBridge(Channel):
    """Channel adapter over the whatsapp-web.js sidecar (experimental)."""

    name = "whatsapp"

    def __init__(self, sidecar_url: str = "http://localhost:18791",
                 poll_interval: float = 3.0,
                 http_timeout: float = 30.0) -> None:
        super().__init__()
        self.sidecar_url = sidecar_url.rstrip("/")
        self.poll_interval = poll_interval
        self.http_timeout = http_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._since = time.time()
        self._seen: set[str] = set()

    # -- Channel interface --------------------------------------------------

    def start(self, on_message) -> None:
        self.on_message = on_message
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="whatsapp-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def send_text(self, peer_id: str, text: str) -> None:
        self._deliver(peer_id, text)

    # -- sidecar transport (override in tests) --------------------------------

    def _fetch_events(self) -> list[dict]:
        resp = httpx.get(f"{self.sidecar_url}/events",
                         params={"since": self._since},
                         timeout=self.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("events", [])

    def _deliver(self, to: str, text: str) -> None:
        resp = httpx.post(f"{self.sidecar_url}/send",
                          json={"to": to, "text": text},
                          timeout=self.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"sidecar send failed: {data}")

    # -- polling ---------------------------------------------------------------

    def poll_once(self) -> None:
        """Fetch and dispatch one batch of sidecar events (tests use this)."""
        for ev in self._fetch_events():
            ev_id = str(ev.get("id", ""))
            if ev_id and ev_id in self._seen:
                continue
            if ev_id:
                self._seen.add(ev_id)
            self._since = max(self._since, float(ev.get("ts", self._since)))
            msg = self._parse(ev)
            if msg is not None and self.on_message is not None:
                self.on_message(msg)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                time.sleep(min(30.0, self.poll_interval * 5))
                continue
            time.sleep(self.poll_interval)

    @staticmethod
    def _parse(ev: dict) -> InboundMessage | None:
        text = ev.get("text")
        if not text:
            return None
        return InboundMessage(
            channel="whatsapp",
            peer_id=str(ev.get("from", "")),
            peer_name=ev.get("name"),
            text=text,
            message_id=str(ev.get("id", "")),
            timestamp=float(ev.get("ts", 0)),
        )

    def health(self) -> dict:
        """Probe the sidecar (used by setup docs / diagnostics)."""
        try:
            resp = httpx.get(f"{self.sidecar_url}/health",
                             timeout=self.http_timeout)
            return {"ok": resp.status_code == 200, "detail": resp.text[:200]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
