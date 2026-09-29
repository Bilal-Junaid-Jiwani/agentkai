"""Messaging gateway: chat channels -> agent sessions.

Run with ``agentkai gateway``. Configuration lives in
``~/.agentkai/channels.yaml`` (see :func:`write_example_config` and
``channels/README.md``).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .core import (Channel, Gateway, InboundMessage, SessionStore,
                   load_config, split_text, write_example_config)

__all__ = [
    "Channel", "Gateway", "InboundMessage", "SessionStore", "load_config",
    "split_text", "write_example_config", "build_channels", "run_gateway",
]


def _secret(cfg: dict, key: str, env_name: str) -> str:
    """Read a credential from config or env. Never logged by callers."""
    return str(cfg.get(key) or os.environ.get(env_name, "") or "")


def build_channels(config: dict) -> dict[str, Channel]:
    """Instantiate every enabled channel from the config dict."""
    channels: dict[str, Channel] = {}
    chan_cfg = config.get("channels") or {}

    tg = chan_cfg.get("telegram", {})
    if tg.get("enabled"):
        from .telegram import TelegramChannel
        channels["telegram"] = TelegramChannel(
            token=_secret(tg, "token", tg.get("token_env",
                                             "TELEGRAM_BOT_TOKEN")))

    dc = chan_cfg.get("discord", {})
    if dc.get("enabled"):
        from .discord import DiscordChannel
        channels["discord"] = DiscordChannel(
            token=_secret(dc, "token", dc.get("token_env",
                                             "DISCORD_BOT_TOKEN")))

    wc = chan_cfg.get("webchat", {})
    if wc.get("enabled"):
        from .webchat import WebChatChannel
        channels["webchat"] = WebChatChannel(
            port=int(wc.get("port", 18790)),
            host=str(wc.get("host", "127.0.0.1")))

    wa = chan_cfg.get("whatsapp", {})
    if wa.get("enabled"):
        from .whatsapp import WhatsAppBridge
        channels["whatsapp"] = WhatsAppBridge(
            sidecar_url=str(wa.get("sidecar_url",
                                   "http://localhost:18791")))

    return channels


def run_gateway(config_path: str | Path | None = None,
                port: int | None = None) -> None:
    """Start the gateway daemon; blocks until Ctrl-C."""
    write_example_config(config_path)
    config = load_config(config_path)
    if port is not None:  # CLI --port overrides the webchat port
        config.setdefault("channels", {}).setdefault("webchat", {})["port"] = port
        config["channels"]["webchat"]["enabled"] = True
    channels = build_channels(config)
    if not channels:
        print("No channels enabled. Edit ~/.agentkai/channels.yaml "
              "(see channels/README.md) and restart.")
        return
    gateway = Gateway(config, channels)
    print(f"agentkai gateway starting: {', '.join(sorted(channels))}")
    for name, ch in channels.items():
        if name == "webchat":
            print(f"  webchat: http://127.0.0.1:{ch.port}/")
    gateway.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        gateway.stop()
