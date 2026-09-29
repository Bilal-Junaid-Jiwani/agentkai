"""Image search skill tools (Openverse API, keyless)."""
from __future__ import annotations

from typing import Any

from agentkai.skills_bundle._common import test_transport
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

BASE = "https://api.openverse.org"
LICENSE_TYPES = ("all", "commercial", "modification")


def get_tools(config: dict | None = None) -> list[Tool]:
    def image_search(query: str, limit: int = 10,
                     license_type: str = "all") -> list | str:
        """Search openly-licensed images by keyword. Returns image URLs,
        source pages, licenses and creators."""
        if not query or not query.strip():
            return "ERROR: query is required"
        if license_type not in LICENSE_TYPES:
            return (f"ERROR: license_type must be one of "
                    f"{', '.join(LICENSE_TYPES)}")
        limit = min(max(int(limit), 1), 50)
        client = HttpClient(BASE, transport=test_transport(config))
        try:
            data = client.request(
                "GET", "/v1/images/",
                params={"q": query.strip(),
                        "page_size": str(limit),
                        "license_type": license_type})
        except HttpError as exc:
            return f"ERROR: image search failed: {exc}"
        results = (data or {}).get("results") or []
        return [{
            "title": r.get("title"),
            "image_url": r.get("url"),
            "source_page": r.get("foreign_landing_url"),
            "license": r.get("license"),
            "creator": r.get("creator"),
            "width": r.get("width"),
            "height": r.get("height"),
        } for r in results]

    return [
        Tool(
            name="image_search",
            description="Keyword search for openly-licensed images "
                        "(Openverse). Returns direct image URLs, source "
                        "pages, licenses and creators. No API key needed.",
            json_schema={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "limit": {"type": "integer"},
                             "license_type": {"type": "string",
                                              "enum": list(LICENSE_TYPES)}},
                         "required": ["query"]},
            risk="low", func=image_search),
    ]


__all__ = ["get_tools"]
