"""Discord channel: discord.py bot.

Setup: create an application at https://discord.com/developers, add a bot,
enable the *Message Content* privileged intent, invite it to your server
(or DM it), set ``DISCORD_BOT_TOKEN``, and allowlist your user id.
See channels/README.md.
"""
from __future__ import annotations

import asyncio
import os
import threading

from .core import Channel, InboundMessage

MAX_TEXT = 2000


class DiscordChannel(Channel):
    """discord.py-based channel.

    The discord.py client runs its own asyncio loop in a thread.
    :meth:`on_discord_message` is the public entry point for inbound
    messages (called by the real client; called directly in tests with a
    duck-typed message).
    """

    name = "discord"

    def __init__(self, token: str | None = None) -> None:
        super().__init__()
        token = token or os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            raise ValueError("Discord token missing: set DISCORD_BOT_TOKEN "
                             "or channels.discord.token")
        self._token = token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._channels: dict[str, object] = {}  # peer_id -> discord channel
        self._ready = threading.Event()

    # -- Channel interface --------------------------------------------------

    def start(self, on_message) -> None:
        import discord

        self.on_message = on_message
        intents = discord.Intents.default()
        intents.message_content = True

        outer = self

        class _Client(discord.Client):
            async def on_ready(self):
                outer._ready.set()

            async def on_message(self, message):  # noqa: N802 (discord API)
                await outer._handle_raw(message)

        self._client = _Client(intents=intents)
        self._thread = threading.Thread(target=self._run_client, daemon=True,
                                        name="discord-client")
        self._thread.start()
        # Don't block startup forever if the token is bad; the client logs it.
        self._ready.wait(timeout=30)

    def _run_client(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._client.start(self._token))
        except Exception:
            pass

    def stop(self) -> None:
        if self._loop and self._client:
            fut = asyncio.run_coroutine_threadsafe(self._client.close(),
                                                   self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def send_text(self, peer_id: str, text: str) -> None:
        self._deliver(peer_id, text)

    # -- inbound ---------------------------------------------------------------

    async def _handle_raw(self, message) -> None:
        """Bridge from the discord.py event to the public handler."""
        if self._client and message.author == self._client.user:
            return  # never reply to ourselves
        # Remember where to send the reply.
        peer = str(message.author.id)
        self._channels[peer] = message.channel
        self.on_discord_message(message)

    def on_discord_message(self, message) -> None:
        """Normalize a discord message and route it. Public for tests."""
        text = getattr(message, "content", "") or ""
        if not text.strip():
            return
        author = message.author
        msg = InboundMessage(
            channel="discord",
            peer_id=str(getattr(author, "id", "")),
            peer_name=getattr(author, "name", None),
            text=text,
            message_id=str(getattr(message, "id", "")),
            timestamp=0.0,
        )
        if self.on_message is not None:
            self.on_message(msg)

    # -- outbound ---------------------------------------------------------------

    def _deliver(self, peer_id: str, text: str) -> None:
        """Send via the remembered discord channel for *peer_id*."""
        channel = self._channels.get(peer_id)
        if channel is None or self._loop is None:
            raise RuntimeError(f"no open discord channel for peer {peer_id}")
        fut = asyncio.run_coroutine_threadsafe(channel.send(text), self._loop)
        fut.result(timeout=30)
