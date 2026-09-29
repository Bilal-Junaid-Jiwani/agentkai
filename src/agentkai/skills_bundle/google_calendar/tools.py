"""Google Calendar skill tools (Calendar API v3)."""
from __future__ import annotations

from typing import Any

from agentkai.skills_bundle._common import (
    auth_error, get_token, test_transport,
)
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

ENV_VAR = "GCAL_TOKEN"
TOKEN_FILE = "gcal_token.json"
BASE = "https://www.googleapis.com/calendar/v3"


def _client(config: dict | None) -> HttpClient | None:
    token = get_token(config, ENV_VAR, TOKEN_FILE)
    if not token:
        return None
    return HttpClient(BASE, headers={"Authorization": f"Bearer {token}"},
                      transport=test_transport(config))


def _no_auth() -> str:
    return auth_error("google_calendar", ENV_VAR, TOKEN_FILE)


def _summarize(event: dict) -> dict:
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": event.get("location", ""),
        "description": (event.get("description") or "")[:500],
        "attendees": [a.get("email") for a in event.get("attendees", []) or []],
        "htmlLink": event.get("htmlLink", ""),
    }


def get_tools(config: dict | None = None) -> list[Tool]:
    def calendar_list(calendar_id: str = "primary",
                      time_min: str = "", time_max: str = "",
                      max_results: int = 20) -> dict | str:
        """List upcoming events (agenda). Times are RFC3339; omit for now→+30d."""
        client = _client(config)
        if client is None:
            return _no_auth()
        params: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(max(int(max_results), 1), 100),
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        try:
            data = client.request(
                "GET", f"/calendars/{calendar_id}/events", params=params)
        except HttpError as exc:
            return f"ERROR: calendar list failed: {exc}"
        items = (data or {}).get("items", []) or []
        return {"events": [_summarize(e) for e in items]}

    def calendar_create(calendar_id: str = "primary", summary: str = "",
                        start: str = "", end: str = "",
                        description: str = "",
                        attendees: list[str] | None = None,
                        timezone: str = "UTC") -> dict | str:
        """Create an event. start/end are RFC3339 (or YYYY-MM-DD all-day)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not summary or not start or not end:
            return "ERROR: summary, start and end are required"
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start, "timeZone": timezone}
                     if "T" in start else {"date": start},
            "end": {"dateTime": end, "timeZone": timezone}
                   if "T" in end else {"date": end},
        }
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        try:
            data = client.request(
                "POST", f"/calendars/{calendar_id}/events", json_body=body)
        except HttpError as exc:
            return f"ERROR: calendar create failed: {exc}"
        return {"event": _summarize(data or {})}

    def calendar_update(calendar_id: str = "primary", event_id: str = "",
                        summary: str = "", start: str = "", end: str = "",
                        description: str = "",
                        timezone: str = "UTC") -> dict | str:
        """Patch an event. Only non-empty fields are changed. HIGH RISK."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not event_id:
            return "ERROR: event_id is required"
        body: dict[str, Any] = {}
        if summary:
            body["summary"] = summary
        if description:
            body["description"] = description
        if start:
            body["start"] = ({"dateTime": start, "timeZone": timezone}
                             if "T" in start else {"date": start})
        if end:
            body["end"] = ({"dateTime": end, "timeZone": timezone}
                           if "T" in end else {"date": end})
        if not body:
            return "ERROR: nothing to update — pass at least one field"
        try:
            data = client.request(
                "PATCH", f"/calendars/{calendar_id}/events/{event_id}",
                json_body=body)
        except HttpError as exc:
            return f"ERROR: calendar update failed: {exc}"
        return {"event": _summarize(data or {})}

    def calendar_delete(calendar_id: str = "primary",
                        event_id: str = "") -> dict | str:
        """Delete an event. HIGH RISK — destructive, always gated."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not event_id:
            return "ERROR: event_id is required"
        try:
            client.request(
                "DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        except HttpError as exc:
            return f"ERROR: calendar delete failed: {exc}"
        return {"ok": True, "deleted": event_id}

    return [
        Tool(
            name="calendar_list",
            description=("List calendar events (agenda) on a calendar. "
                         "time_min/time_max are RFC3339; omit both for the "
                         "coming 30 days. Default calendar is 'primary'."),
            json_schema={"type": "object",
                         "properties": {
                             "calendar_id": {"type": "string"},
                             "time_min": {"type": "string"},
                             "time_max": {"type": "string"},
                             "max_results": {"type": "integer"}},
                         },
            risk="low",
            func=calendar_list,
        ),
        Tool(
            name="calendar_create",
            description=("Create a calendar event. start/end are RFC3339 "
                         "datetimes with timezone offset "
                         "(2026-10-01T14:00:00+05:00) or YYYY-MM-DD for "
                         "all-day events."),
            json_schema={"type": "object",
                         "properties": {
                             "calendar_id": {"type": "string"},
                             "summary": {"type": "string"},
                             "start": {"type": "string"},
                             "end": {"type": "string"},
                             "description": {"type": "string"},
                             "attendees": {"type": "array",
                                           "items": {"type": "string"}},
                             "timezone": {"type": "string"}},
                         "required": ["summary", "start", "end"]},
            risk="medium",
            func=calendar_create,
        ),
        Tool(
            name="calendar_update",
            description=("Patch a calendar event; only non-empty fields "
                         "change. HIGH RISK: modifies the user's schedule."),
            json_schema={"type": "object",
                         "properties": {
                             "calendar_id": {"type": "string"},
                             "event_id": {"type": "string"},
                             "summary": {"type": "string"},
                             "start": {"type": "string"},
                             "end": {"type": "string"},
                             "description": {"type": "string"},
                             "timezone": {"type": "string"}},
                         "required": ["event_id"]},
            risk="high",
            func=calendar_update,
        ),
        Tool(
            name="calendar_delete",
            description=("Delete a calendar event. HIGH RISK: destructive "
                         "and not undoable through this skill."),
            json_schema={"type": "object",
                         "properties": {
                             "calendar_id": {"type": "string"},
                             "event_id": {"type": "string"}},
                         "required": ["event_id"]},
            risk="high",
            func=calendar_delete,
        ),
    ]


__all__ = ["get_tools", "ENV_VAR", "TOKEN_FILE"]
