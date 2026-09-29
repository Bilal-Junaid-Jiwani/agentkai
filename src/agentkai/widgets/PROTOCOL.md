# Widget protocol v1

Interactive chat widgets let the agent show rich UI inside a chat: tappable
option buttons, small forms, info cards, and map pins. The protocol is
deliberately tiny — the **agent produces JSON, the client renders it**.
AgentKai never dictates pixels.

## Envelope

Every widget is a JSON object:

```json
{
  "version": 1,
  "type": "options",
  ...
}
```

- `version` — protocol version. Clients must reject (with a visible error)
  any payload whose version they don't speak. Current: **1**.
- `type` — one of `options`, `form`, `card`, `map`.

## Types

### options — tappable buttons

```json
{
  "version": 1,
  "type": "options",
  "title": "Pick a model (optional)",
  "question": "Which model should handle this?",
  "allow_multiple": false,
  "options": [
    {"id": "claude", "label": "Claude", "description": "best reasoning (optional)"},
    {"id": "local", "label": "Local Ollama"}
  ]
}
```

On tap the client sends back the chosen `id`(s) as a normal user message,
e.g. `widget:opt-3` or the label text — the client decides, but it must be
documented in the client's own UI notes. `allow_multiple: true` renders
checkboxes instead of radio buttons.

### form — small input form

```json
{
  "version": 1,
  "type": "form",
  "title": "Book a table",
  "submit_label": "Book",
  "fields": [
    {"id": "name", "type": "text", "label": "Name", "required": true,
     "placeholder": "Bilal"},
    {"id": "guests", "type": "number", "label": "Guests", "default": 2},
    {"id": "date", "type": "date", "label": "Date", "required": true},
    {"id": "slot", "type": "select", "label": "Slot",
     "options": [{"id": "lunch", "label": "Lunch"}, {"id": "dinner", "label": "Dinner"}]},
    {"id": "window_seat", "type": "boolean", "label": "Window seat"}
  ]
}
```

Field types: `text`, `number`, `date`, `select`, `boolean`. On submit the
client sends back `{"widget": "form", "values": {"name": ..., ...}}` as the
user's reply.

### card — info card

```json
{
  "version": 1,
  "type": "card",
  "title": "Flight PK-304",
  "body": "Karachi → Lahore · departs 18:40 · on time.",
  "image_url": "https://... (optional)",
  "actions": [
    {"id": "track", "label": "Track", "style": "primary"},
    {"id": "cancel", "label": "Cancel booking", "style": "danger"}
  ]
}
```

Action styles: `primary`, `secondary` (default), `danger`. Tapping an
action sends back its `id` like an option tap.

### map — map pin

```json
{
  "version": 1,
  "type": "map",
  "latitude": 24.8607,
  "longitude": 67.0011,
  "label": "Karachi",
  "zoom": 14
}
```

Clients render with their own map tiles/provider. The agent never embeds
map images — just coordinates.

## Client duties

1. Validate `version` and `type` before rendering; show a fallback text
   message for anything unrecognized (never crash the chat).
2. Never execute anything from a widget payload — no URLs are fetched
   implicitly, no scripts run. `image_url` on cards is the one exception
   and must be loaded as an image only.
3. Keep the interaction loop: the user's tap/submit becomes their next
   message to the agent, so the agent can react.
