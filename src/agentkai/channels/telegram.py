"""Telegram channel: Bot API via httpx long-polling.

Setup: create a bot with @BotFather, set ``TELEGRAM_BOT_TOKEN`` (or
``token`` in channels.yaml), and put your chat id in the channel's
allowlist. See channels/README.md.
"""
from __future__ import annotations

import os
import threading
import time

import httpx

from .core import Channel, InboundMessage

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096
POLL_TIMEOUT = 50  # seconds for getUpdates long-poll


class TelegramChannel(Channel):
    """Telegram Bot API channel.

    Network I/O is isolated in :meth:`_api` and :meth:`_fetch_updates` so
    tests can subclass with fake transports.
    """

    name = "telegram"

    def __init__(self, token: str | None = None,
                 poll_interval: float = 1.0,
                 http_timeout: float = POLL_TIMEOUT + 10) -> None:
        super().__init__()
        token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise ValueError("Telegram token missing: set TELEGRAM_BOT_TOKEN "
                             "or channels.telegram.token")
        self._token = token
        self.poll_interval = poll_interval
        self.http_timeout = http_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    # -- transport (override in tests) ------------------------------------

    def _api(self, method: str, **params) -> dict:
        """One Bot API call. Returns the decoded JSON ``result``."""
        url = API.format(token=self._token, method=method)
        # Never log the token; the URL carries it in the path.
        resp = httpx.post(url, json=params, timeout=self.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data["result"]

    def _fetch_updates(self) -> list[dict]:
        return self._api("getUpdates", offset=self._offset,
                         timeout=POLL_TIMEOUT)

    def _deliver(self, chat_id: str, text: str) -> None:
        self._api("sendMessage", chat_id=chat_id, text=text)

    # -- Channel interface --------------------------------------------------

    def start(self, on_message) -> None:
        self.on_message = on_message
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="telegram-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def send_text(self, peer_id: str, text: str) -> None:
        self._deliver(peer_id, text)

    # -- polling -------------------------------------------------------------

    def poll_once(self) -> None:
        """Fetch and dispatch one batch of updates (used by start + tests)."""
        for update in self._fetch_updates():
            self._offset = max(self._offset, update.get("update_id", 0) + 1)
            msg = self._parse(update)
            if msg is not None and self.on_message is not None:
                self.on_message(msg)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # Transient network/API failure: back off, keep polling.
                time.sleep(min(30.0, self.poll_interval * 5))
                continue
            if self.poll_interval:
                time.sleep(self.poll_interval)

    @staticmethod
    def _parse(update: dict) -> InboundMessage | None:
        """Normalize a getUpdates entry to InboundMessage (text only)."""
        message = update.get("message") or update.get("edited_message") or {}
        text = message.get("text")
        if not text:
            return None  # stickers/photos/etc. are ignored, not errors
        chat = message.get("chat", {})
        frm = message.get("from", {})
        name = frm.get("username") or frm.get("first_name")
        return InboundMessage(
            channel="telegram",
            peer_id=str(chat.get("id", "")),
            peer_name=name,
            text=text,
            message_id=str(message.get("message_id", "")),
            timestamp=float(message.get("date", 0)),
        )
