# Channels — the messaging gateway

`agentkai gateway` connects chat apps to the agent. One daemon, many
channels; every conversation is an isolated session with its own agent
run and event log. Chat from your phone, get answers and approvals from
wherever you are.

## Quick start

```bash
agentkai gateway
```

Starts with WebChat enabled on `http://127.0.0.1:18790/` — open it in a
browser. The first run writes an annotated `~/.agentkai/channels.yaml`;
edit it to enable channels, then restart.

## Channels

| Channel | Transport | Setup |
|---|---|---|
| **WebChat** | Built-in WebSocket page, localhost only | none — on by default |
| **Telegram** | Bot API, long-polling | bot token from [@BotFather](https://t.me/BotFather) |
| **Discord** | discord.py bot | application + bot token, *Message Content* intent |
| **WhatsApp** | **Experimental** bridge via a Node sidecar | `whatsapp-sidecar`, scan QR |

Details per channel live in `src/agentkai/channels/README.md` (installed
with the package) — including the sidecar setup for WhatsApp.

## Config (`~/.agentkai/channels.yaml`)

```yaml
owner: "telegram:123456"     # who gets approval requests (channel:peer)
approval_timeout: 300        # seconds the owner has to APPROVE/DENY
model: claude                # model alias for chat sessions
channels:
  telegram: {enabled: true, token_env: TELEGRAM_BOT_TOKEN,
             allowlist: ["123456"], owner_peer: "123456"}
  discord:  {enabled: false, token_env: DISCORD_BOT_TOKEN,
             allowlist: ["789"], owner_peer: "789"}
  webchat:  {enabled: true, port: 18790, allowlist: ["*"], owner_peer: "local"}
  whatsapp: {enabled: false, sidecar_url: "http://localhost:18791",
             allowlist: ["+15551234567"], owner_peer: "+15551234567"}
```

- **allowlist**: peer ids allowed to talk to the agent. Unknown senders
  are logged and get no reply. `"*"` allows anyone — only sensible for
  WebChat, which binds 127.0.0.1.
- **owner_peer**: the owner's id on that channel. Only the owner can
  answer approval requests.
- Tokens come from the environment (`token_env`) or `token:` in the file.
  Prefer the environment; never commit the file.

## Sessions

Session key = `channel:peer` (e.g. `telegram:123456`), or `linked:<name>`
when `identity_links` maps your ids across channels to one session.
History lives in `~/.agentkai/sessions.db`; every message is also
appended to the session's event log. While a session's agent is busy,
new messages get a "still working…" reply instead of queueing.

## Approvals in chat

Medium/high-risk tools ask the **owner**:

```
Approval needed [a1b2c3d4]:
exec(command='rm -rf /tmp/x') [risk=high]
Reply `APPROVE a1b2c3d4` or `DENY a1b2c3d4`.
```

No reply within `approval_timeout` = deny. Non-owners cannot approve.
There is deliberately no auto-approve in gateway mode.

## Voice notes

Channels download incoming voice notes and transcribe them before the
agent sees the message ([voice.md](voice.md)). Transcription failures
arrive as readable errors, not crashes.

## Widgets

The agent can send interactive UI — option buttons, small forms, info
cards, map pins — via the `render_widget` tool. Each channel renders
them natively (Telegram buttons, Discord embeds, …). See the widget
protocol in `src/agentkai/widgets/PROTOCOL.md`.

## Limits

- Text-first: image/voice **output** in chat is not in v1; non-text
  inbound Telegram/Discord messages are ignored (voice notes excepted).
- Long replies are split per channel (Telegram 4096, Discord 2000 chars).
- Gateway runs share `~/.agentkai` files (memory, config) with the CLI
  and dashboard, but not live session state.
- **WhatsApp is experimental**: it bridges through the `whatsapp-web.js`
  sidecar you run yourself (Node required); WhatsApp may rate-limit
  automated web clients — use a spare number.
