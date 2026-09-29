"""Media skill tools: thin wrappers over agentkai.media."""
from __future__ import annotations

from agentkai.media import media_tools


def get_tools(config: dict | None = None):
    """Return the generate_image / generate_video / edit_image tools."""
    return media_tools(config)
