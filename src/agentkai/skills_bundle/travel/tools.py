"""Travel skill tools: OpenSky flight tracking + pluggable providers.

Honest design: live flight tracking is bundled and real. Flight/hotel
*search* needs a user-registered provider — see SKILL.md.
"""
from __future__ import annotations

from typing import Any, Callable

from agentkai.skills_bundle._common import test_transport
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

OPENSKY_BASE = "https://opensky-network.org"

# Provider contracts:
#   flights(origin, destination, date) -> list[dict{airline, flight_no,
#       departs, arrives, price, currency, booking_url}]
#   hotels(location, checkin, checkout) -> list[dict{name, price_per_night,
#       currency, rating, booking_url}]
_registry: dict[str, dict[str, Callable]] = {}


def register_provider(name: str,
                      flights: Callable | None = None,
                      hotels: Callable | None = None) -> None:
    """Register a travel provider (code-level registration)."""
    if not name or not name.strip():
        raise ValueError("provider name is required")
    entry: dict[str, Callable] = {}
    if flights is not None:
        if not callable(flights):
            raise ValueError("flights must be callable")
        entry["flights"] = flights
    if hotels is not None:
        if not callable(hotels):
            raise ValueError("hotels must be callable")
        entry["hotels"] = hotels
    if not entry:
        raise ValueError("register at least one of flights/hotels")
    _registry[name.strip()] = entry


def _providers(config: dict | None) -> dict[str, dict[str, Callable]]:
    providers: dict[str, dict[str, Callable]] = {
        name: dict(entry) for name, entry in _registry.items()}
    cfg = (config or {}).get("travel_providers") or {}
    for name, entry in cfg.items():
        if isinstance(entry, dict):
            clean = {k: v for k, v in entry.items()
                     if k in ("flights", "hotels") and callable(v)}
            if clean:
                providers[name] = clean
    return providers


def _no_provider(kind: str) -> str:
    return (
        f"ERROR: no {kind} search provider is configured. AgentKai does "
        f"not bundle flight/hotel booking — public booking APIs need "
        f"commercial keys. Register one with "
        f"travel.tools.register_provider(...) or via the 'travel_providers' "
        f"skill config (see the travel SKILL.md). Live flight *tracking* "
        f"via travel_flight_status works without any provider.")


def get_tools(config: dict | None = None) -> list[Tool]:
    def travel_flight_status(icao24: str = "",
                             callsign: str = "") -> dict | str:
        """Live ADS-B track for one aircraft (OpenSky, keyless)."""
        icao24 = (icao24 or "").strip().lower()
        callsign = (callsign or "").strip().upper()
        if not icao24 and not callsign:
            return "ERROR: icao24 or callsign is required"
        client = HttpClient(OPENSKY_BASE, transport=test_transport(config))
        try:
            data = client.request("GET", "/api/states/all")
        except HttpError as exc:
            return f"ERROR: flight status lookup failed: {exc}"
        states = (data or {}).get("states") or []
        match = None
        for s in states:
            if not isinstance(s, list) or len(s) < 13:
                continue
            sid = str(s[0] or "").lower()
            scal = str(s[1] or "").strip().upper()
            if (icao24 and sid == icao24) or (callsign and scal == callsign):
                match = s
                break
        if match is None:
            return ("ERROR: no live ADS-B track found for "
                    f"{icao24 or callsign} right now (aircraft may be out "
                    f"of receiver coverage or on the ground without "
                    f"transponder).")
        return {
            "icao24": match[0],
            "callsign": (match[1] or "").strip(),
            "origin_country": match[2],
            "lat": match[6],
            "lon": match[5],
            "altitude_m": match[7],
            "on_ground": bool(match[8]),
            "velocity_ms": match[9],
            "heading_deg": match[10],
            "vertical_rate_ms": match[11],
            "last_contact_unix": match[4],
        }

    def travel_search_flights(origin: str, destination: str, date: str,
                              provider: str = "") -> list | str:
        """Search flights via a registered provider (none bundled)."""
        if not (origin and origin.strip() and destination
                and destination.strip() and date and date.strip()):
            return "ERROR: origin, destination and date are required"
        providers = _providers(config)
        entry = providers.get((provider or "").strip())
        fn = (entry or {}).get("flights")
        if fn is None:
            return _no_provider("flight")
        try:
            items = fn(origin.strip(), destination.strip(), date.strip())
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: provider raised: {exc}"
        if not isinstance(items, list):
            return "ERROR: provider returned a non-list result"
        return items

    def travel_search_hotels(location: str, checkin: str, checkout: str,
                             provider: str = "") -> list | str:
        """Search hotels via a registered provider (none bundled)."""
        if not (location and location.strip() and checkin
                and checkin.strip() and checkout and checkout.strip()):
            return "ERROR: location, checkin and checkout are required"
        providers = _providers(config)
        entry = providers.get((provider or "").strip())
        fn = (entry or {}).get("hotels")
        if fn is None:
            return _no_provider("hotel")
        try:
            items = fn(location.strip(), checkin.strip(), checkout.strip())
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: provider raised: {exc}"
        if not isinstance(items, list):
            return "ERROR: provider returned a non-list result"
        return items

    return [
        Tool(
            name="travel_flight_status",
            description="Live ADS-B position/altitude/velocity for one "
                        "aircraft via OpenSky (keyless). Pass icao24 "
                        "(hex id) or callsign.",
            json_schema={"type": "object",
                         "properties": {
                             "icao24": {"type": "string"},
                             "callsign": {"type": "string"}},
                         "required": []},
            risk="low", func=travel_flight_status),
        Tool(
            name="travel_search_flights",
            description="Search flight availability via a registered "
                        "provider. No provider is bundled — without one "
                        "this returns a configuration error; see SKILL.md.",
            json_schema={"type": "object",
                         "properties": {
                             "origin": {"type": "string"},
                             "destination": {"type": "string"},
                             "date": {"type": "string",
                                      "description": "YYYY-MM-DD"},
                             "provider": {"type": "string"}},
                         "required": ["origin", "destination", "date"]},
            risk="low", func=travel_search_flights),
        Tool(
            name="travel_search_hotels",
            description="Search hotel availability via a registered "
                        "provider. No provider is bundled — without one "
                        "this returns a configuration error; see SKILL.md.",
            json_schema={"type": "object",
                         "properties": {
                             "location": {"type": "string"},
                             "checkin": {"type": "string",
                                         "description": "YYYY-MM-DD"},
                             "checkout": {"type": "string",
                                          "description": "YYYY-MM-DD"},
                             "provider": {"type": "string"}},
                         "required": ["location", "checkin", "checkout"]},
            risk="low", func=travel_search_hotels),
    ]


__all__ = ["get_tools", "register_provider"]
