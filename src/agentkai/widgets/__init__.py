"""Interactive chat widgets: rich in-chat UI the agent can emit.

Channels (WhatsApp/Telegram/Discord/WebChat) and the dashboard render
native UI from a small versioned JSON protocol. The agent never renders
anything itself — it produces a widget payload with the builders below
(or the ``render_widget`` tool), and the client decides how to display it.

Protocol version: 1. Full spec: ``src/agentkai/widgets/PROTOCOL.md``.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from ..tools import Tool

PROTOCOL_VERSION = 1

WIDGET_TYPES = ("options", "form", "card", "map")

FORM_FIELD_TYPES = ("text", "number", "date", "select", "boolean")


class WidgetError(Exception):
    """Invalid widget payload."""


# ---- builders ---------------------------------------------------------------

def create_options(question: str, options: list[dict],
                   allow_multiple: bool = False,
                   title: str | None = None) -> dict:
    """Tappable option buttons. Each option: {"id", "label", "description"?}."""
    question = question.strip()
    if not question:
        raise WidgetError("options widget needs a non-empty question")
    if not options:
        raise WidgetError("options widget needs at least one option")
    norm = []
    seen = set()
    for i, opt in enumerate(options):
        if not isinstance(opt, dict):
            raise WidgetError(f"option {i} must be a dict")
        oid = str(opt.get("id", f"opt-{i}")).strip()
        label = str(opt.get("label", "")).strip()
        if not oid or not label:
            raise WidgetError(f"option {i} needs non-empty 'id' and 'label'")
        if oid in seen:
            raise WidgetError(f"duplicate option id {oid!r}")
        seen.add(oid)
        item: dict[str, Any] = {"id": oid, "label": label}
        if opt.get("description"):
            item["description"] = str(opt["description"])
        norm.append(item)
    payload: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "type": "options",
        "question": question,
        "options": norm,
        "allow_multiple": bool(allow_multiple),
    }
    if title:
        payload["title"] = title
    return payload


def create_form(title: str, fields: list[dict],
                submit_label: str = "Submit") -> dict:
    """A small input form. Each field: {"id", "type", "label", ...}.

    Types: text | number | date | select | boolean.
    Optional keys: required (bool), placeholder, options (for select —
    same shape as create_options options), default.
    """
    title = title.strip()
    if not title:
        raise WidgetError("form widget needs a non-empty title")
    if not fields:
        raise WidgetError("form widget needs at least one field")
    norm = []
    seen = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise WidgetError(f"field {i} must be a dict")
        fid = str(f.get("id", "")).strip()
        ftype = str(f.get("type", "")).strip()
        label = str(f.get("label", "")).strip()
        if not fid or not label:
            raise WidgetError(f"field {i} needs non-empty 'id' and 'label'")
        if ftype not in FORM_FIELD_TYPES:
            raise WidgetError(
                f"field {fid!r}: unknown type {ftype!r} "
                f"(allowed: {', '.join(FORM_FIELD_TYPES)})")
        if fid in seen:
            raise WidgetError(f"duplicate field id {fid!r}")
        seen.add(fid)
        item: dict[str, Any] = {
            "id": fid, "type": ftype, "label": label,
            "required": bool(f.get("required", False)),
        }
        for key in ("placeholder", "default"):
            if f.get(key) is not None:
                item[key] = f[key]
        if ftype == "select":
            item["options"] = create_options(
                "select", f.get("options", []))["options"]
        norm.append(item)
    return {
        "version": PROTOCOL_VERSION,
        "type": "form",
        "title": title,
        "fields": norm,
        "submit_label": submit_label,
    }


def create_card(title: str, body: str,
                actions: list[dict] | None = None,
                image_url: str | None = None) -> dict:
    """An info card with optional action buttons and an image.

    Actions: [{"id", "label", "style"?}] where style is
    "primary" | "secondary" | "danger" (default "secondary").
    """
    title = title.strip()
    if not title:
        raise WidgetError("card widget needs a non-empty title")
    norm_actions = []
    for i, a in enumerate(actions or []):
        if not isinstance(a, dict):
            raise WidgetError(f"action {i} must be a dict")
        aid = str(a.get("id", "")).strip()
        label = str(a.get("label", "")).strip()
        if not aid or not label:
            raise WidgetError(f"action {i} needs non-empty 'id' and 'label'")
        style = str(a.get("style", "secondary"))
        if style not in ("primary", "secondary", "danger"):
            raise WidgetError(f"action {aid!r}: unknown style {style!r}")
        norm_actions.append({"id": aid, "label": label, "style": style})
    payload: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "type": "card",
        "title": title,
        "body": body,
    }
    if norm_actions:
        payload["actions"] = norm_actions
    if image_url:
        payload["image_url"] = image_url
    return payload


def create_map(latitude: float, longitude: float, label: str = "",
               zoom: int = 14) -> dict:
    """A map pin. Clients render with their own map tiles."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        raise WidgetError("map widget needs numeric latitude/longitude")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WidgetError("latitude must be -90..90, longitude -180..180")
    zoom = max(1, min(int(zoom), 20))
    return {
        "version": PROTOCOL_VERSION,
        "type": "map",
        "latitude": lat,
        "longitude": lon,
        "label": label,
        "zoom": zoom,
    }


# ---- validation --------------------------------------------------------------

def validate_widget(payload: Any) -> dict:
    """Validate a widget payload; return it unchanged or raise WidgetError."""
    if not isinstance(payload, dict):
        raise WidgetError("widget payload must be a JSON object")
    if payload.get("version") != PROTOCOL_VERSION:
        raise WidgetError(
            f"unsupported widget protocol version "
            f"{payload.get('version')!r} (this client speaks "
            f"{PROTOCOL_VERSION})")
    wtype = payload.get("type")
    if wtype not in WIDGET_TYPES:
        raise WidgetError(f"unknown widget type {wtype!r}")
    required = {
        "options": ("question", "options"),
        "form": ("title", "fields"),
        "card": ("title", "body"),
        "map": ("latitude", "longitude"),
    }[wtype]
    missing = [k for k in required if k not in payload]
    if missing:
        raise WidgetError(
            f"widget type {wtype!r} missing keys: {', '.join(missing)}")
    return payload


# ---- agent tool ---------------------------------------------------------------

def widget_tools() -> list[Tool]:
    """The ``render_widget`` tool: validates and returns a widget payload.

    The return value is the widget JSON itself — channels and the dashboard
    detect it in tool results and render it natively. Low risk: it only
    formats data, it never acts.
    """

    def _render_widget(widget: dict) -> dict:
        try:
            payload = validate_widget(widget)
        except WidgetError as exc:
            return f"ERROR: {exc}"
        # Re-serialize through JSON to guarantee it is JSON-safe.
        return json.loads(json.dumps(payload))

    return [
        Tool(
            name="render_widget",
            description=(
                "Emit an interactive widget (option buttons, form, info "
                "card, or map) for the user. Build the payload with the "
                "widget helpers: type 'options' needs question+options, "
                "'form' needs title+fields, 'card' needs title+body, "
                "'map' needs latitude+longitude. The chat client renders it."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "widget": {
                        "type": "object",
                        "description": "widget JSON payload, protocol v1",
                    },
                },
                "required": ["widget"],
            },
            risk="low",
            func=_render_widget,
        ),
    ]


__all__ = ["create_options", "create_form", "create_card", "create_map",
           "validate_widget", "widget_tools", "WidgetError",
           "PROTOCOL_VERSION", "WIDGET_TYPES"]
