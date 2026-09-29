"""Voice skill tools: transcribe_audio + speak_text."""
from __future__ import annotations

from agentkai.tools import Tool
from agentkai.voice import voice_tools


def get_tools(config: dict | None = None) -> list[Tool]:
    return voice_tools(config)
