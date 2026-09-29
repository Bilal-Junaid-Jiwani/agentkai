"""Tests for widget builders, validation, and the render_widget tool."""
import json

import pytest

from agentkai.widgets import (WidgetError, create_card, create_form,
                              create_map, create_options, validate_widget,
                              widget_tools)


def test_options_builder_schema():
    w = create_options("Pick one", [
        {"id": "a", "label": "Alpha", "description": "first"},
        {"id": "b", "label": "Beta"},
    ])
    assert validate_widget(w) is w
    assert w["version"] == 1 and w["type"] == "options"
    assert w["options"][0] == {"id": "a", "label": "Alpha",
                               "description": "first"}
    assert w["allow_multiple"] is False
    json.dumps(w)  # JSON-safe


def test_options_rejects_bad_input():
    with pytest.raises(WidgetError):
        create_options("  ", [{"id": "a", "label": "A"}])
    with pytest.raises(WidgetError):
        create_options("q", [])
    with pytest.raises(WidgetError):
        create_options("q", [{"id": "a", "label": "A"},
                             {"id": "a", "label": "dup"}])
    with pytest.raises(WidgetError):
        create_options("q", [{"id": "a"}])  # no label


def test_form_builder_all_field_types():
    w = create_form("Book", [
        {"id": "name", "type": "text", "label": "Name", "required": True},
        {"id": "n", "type": "number", "label": "N", "default": 2},
        {"id": "d", "type": "date", "label": "Date"},
        {"id": "s", "type": "select", "label": "Slot",
         "options": [{"id": "am", "label": "AM"}]},
        {"id": "w", "type": "boolean", "label": "Window"},
    ], submit_label="Go")
    assert validate_widget(w) is w
    assert w["submit_label"] == "Go"
    assert w["fields"][3]["options"] == [{"id": "am", "label": "AM"}]
    with pytest.raises(WidgetError):
        create_form("t", [{"id": "x", "type": "color", "label": "X"}])


def test_card_and_map():
    w = create_card("Title", "Body here",
                    actions=[{"id": "ok", "label": "OK", "style": "primary"}],
                    image_url="https://example.com/i.png")
    assert validate_widget(w) is w
    assert w["actions"][0]["style"] == "primary"
    with pytest.raises(WidgetError):
        create_card("t", "b", actions=[{"id": "x", "label": "Y",
                                        "style": "neon"}])
    m = create_map(24.8607, 67.0011, label="Karachi", zoom=14)
    assert validate_widget(m) is m
    assert m["latitude"] == 24.8607
    with pytest.raises(WidgetError):
        create_map(999, 0)


def test_validate_rejects_unknown_version_and_type():
    with pytest.raises(WidgetError):
        validate_widget({"version": 99, "type": "options"})
    with pytest.raises(WidgetError):
        validate_widget({"version": 1, "type": "hologram"})
    with pytest.raises(WidgetError):
        validate_widget({"version": 1, "type": "card", "title": "t"})
    with pytest.raises(WidgetError):
        validate_widget("not a dict")


def test_render_widget_tool():
    tools = {t.name: t for t in widget_tools()}
    tool = tools["render_widget"]
    assert tool.risk == "low"
    payload = create_options("Tea?", [{"id": "y", "label": "Yes"}])
    out = tool.run(widget=payload)
    assert out["type"] == "options" and out["question"] == "Tea?"
    bad = tool.run(widget={"version": 1, "type": "nope"})
    assert bad.startswith("ERROR:")
