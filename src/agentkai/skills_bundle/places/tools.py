"""Places skill tools (OpenStreetMap Nominatim + Overpass)."""
from __future__ import annotations

import time
import urllib.parse
from typing import Any

from agentkai.skills_bundle._common import test_transport
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
OVERPASS_BASE = "https://overpass-api.de"

# Nominatim usage policy: max ~1 request/second. Enforced client-side.
_NOMINATIM_MIN_GAP = 1.1
_last_nominatim_call = 0.0


def _polite_delay() -> None:
    global _last_nominatim_call
    now = time.monotonic()
    wait = _NOMINATIM_MIN_GAP - (now - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    _last_nominatim_call = time.monotonic()


def _nominatim(config: dict | None) -> HttpClient:
    return HttpClient(NOMINATIM_BASE, transport=test_transport(config))


def _overpass(config: dict | None) -> HttpClient:
    return HttpClient(OVERPASS_BASE, transport=test_transport(config))


def _short_address(addr: dict) -> str:
    parts = [addr.get(k) for k in ("road", "suburb", "city", "town",
                                   "village", "state", "country")
             if addr.get(k)]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ", ".join(seen)


def get_tools(config: dict | None = None) -> list[Tool]:
    def places_search(query: str, near: str = "",
                      limit: int = 10) -> list | str:
        """Search places by name/keyword, optionally near a location."""
        if not query or not query.strip():
            return "ERROR: query is required"
        limit = min(max(int(limit), 1), 25)
        q = query.strip() + (f", {near.strip()}" if near.strip() else "")
        _polite_delay()
        try:
            data = _nominatim(config).request(
                "GET", "/search",
                params={"q": q, "format": "jsonv2",
                        "addressdetails": "1", "limit": str(limit)})
        except HttpError as exc:
            return f"ERROR: places search failed: {exc}"
        out = []
        for item in (data or []):
            addr = item.get("address") or {}
            out.append({
                "name": item.get("name") or (item.get("display_name") or "")
                .split(",")[0],
                "display_name": item.get("display_name"),
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "category": item.get("class"),
                "type": item.get("type"),
                "osm_type": item.get("osm_type"),
                "osm_id": item.get("osm_id"),
                "address": _short_address(addr),
            })
        return out

    def place_details(osm_type: str, osm_id: int) -> dict | str:
        """Full OSM tags for one place: hours, phone, website, address."""
        if osm_type not in ("node", "way", "relation"):
            return ("ERROR: osm_type must be one of "
                    "node, way, relation")
        try:
            oid = int(osm_id)
        except (TypeError, ValueError):
            return "ERROR: osm_id must be an integer"
        ql = f"[out:json];{osm_type}({oid});out tags;"
        try:
            data = _overpass(config).request(
                "POST", "/api/interpreter",
                raw_body=urllib.parse.urlencode(
                    {"data": ql}).encode("utf-8"),
                headers={"Content-Type":
                         "application/x-www-form-urlencoded"})
        except HttpError as exc:
            return f"ERROR: place details failed: {exc}"
        elements = (data or {}).get("elements") or []
        if not elements:
            return (f"ERROR: no OSM {osm_type}/{oid} found "
                    f"(it may have been deleted)")
        el = elements[0]
        tags = el.get("tags") or {}
        addr = {k[5:]: v for k, v in tags.items()
                if k.startswith("addr:") and v}
        return {
            "name": tags.get("name"),
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat"),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon"),
            "opening_hours": tags.get("opening_hours"),
            "phone": tags.get("phone"),
            "website": tags.get("website"),
            "cuisine": tags.get("cuisine"),
            "category": tags.get("amenity") or tags.get("shop")
            or tags.get("tourism") or tags.get("leisure"),
            "address": addr,
            "tags": tags,
        }

    return [
        Tool(
            name="places_search",
            description="Search OpenStreetMap places by keyword, optionally "
                        "near a location. Returns names, coordinates, OSM "
                        "type/id and short addresses. No API key needed.",
            json_schema={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "near": {"type": "string"},
                             "limit": {"type": "integer"}},
                         "required": ["query"]},
            risk="low", func=places_search),
        Tool(
            name="place_details",
            description="Full details for one OSM place (opening hours, "
                        "phone, website, address tags) via the Overpass API. "
                        "osm_type is node, way or relation.",
            json_schema={"type": "object",
                         "properties": {
                             "osm_type": {"type": "string",
                                          "enum": ["node", "way",
                                                   "relation"]},
                             "osm_id": {"type": "integer"}},
                         "required": ["osm_type", "osm_id"]},
            risk="low", func=place_details),
    ]


__all__ = ["get_tools"]
