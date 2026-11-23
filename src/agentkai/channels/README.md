# agentkai messaging gateway

`agentkai gateway` connects chat apps to the agent. One daemon, many
channels; every conversation is an isolated session with its own agent
run and event log.

## Quick start

```bash
agentkai gateway            # starts with webchat enabled (localhost only)
# open http://127.0.0.1:18790/ in a browser
```

The first run writes an annotated `~/.agentkai/channels.yaml`. Edit it to
enable channels, then restart.

## Config reference (`~/.agentkai/channels.yaml`)

```yaml
owner: "telegram:123456"     # who gets approval requests (channel:peer)
approval_timeout: 300        # seconds the owner has to APPROVE/DENY
model: claude                # model alias for chat sessions
auto_approve: false          # (reserved) never auto-approve in gateway mode
identity_links:              # same person across channels -> one session
  owner:
    - "telegram:123456"
    - "discord:789"
    - "whatsapp:+15551234567"
channels:
  telegram: {enabled: false, token_env: TELEGRAM_BOT_TOKEN,
             allowlist: ["123456"], owner_peer: "123456"}
  discord:  {enabled: false, token_env: DISCORD_BOT_TOKEN,
             allowlist: ["789"], owner_peer: "789"}
  webchat:  {enabled: true, port: 18790, allowlist: [], owner_peer: ""}
  whatsapp: {enabled: false, sidecar_url: "http://localhost:18791",
             allowlist: ["+15551234567"], owner_peer: "+15551234567"}
```

- **allowlist**: peer ids allowed to talk to the agent. Unknown senders are
  logged and get no reply. `"*"` allows anyone — avoid it even for webchat:
  it binds 127.0.0.1, but any local process or browser page can reach that
  port and claim any peer id. Every channel ships default-deny (`[]`).
- **owner_peer**: the owner's id on that channel. The owner may also reply
  `APPROVE <id>` / `DENY <id>` to pending tool approvals.
- Tokens come from the environment (`token_env`) or `token:` in the file.
  Prefer the environment; never commit the file with tokens in it.

## Sessions

- Session key = `channel:peer` (e.g. `telegram:123456`), or
  `linked:<name>` when identity linking matches.
- History lives in `~/.agentkai/sessions.db`; every inbound/outbound
  message is also appended to the session's event log
  (`~/.agentkai/runs/gateway-<session>/events.jsonl`).
- While a session's agent is busy, new messages get a "still working…"
  reply instead of queueing.

## Approvals

Medium/high-risk tools in chat mode ask the owner: the owner receives

```
Approval needed [a1b2c3d4]:
exec(command='rm -rf /tmp/x') [risk=high]
Reply `APPROVE a1b2c3d4` or `DENY a1b2c3d4`.
```

No reply within `approval_timeout` = deny. Non-owners cannot approve.

## Channel setup

### Telegram

1. Message @BotFather → `/newbot` → copy the token.
2. `export TELEGRAM_BOT_TOKEN=...`
3. Message your bot once, then find your chat id
   (`https://api.telegram.org/bot<TOKEN>/getUpdates` shows it).
4. Put the id in `allowlist`/`owner_peer`, set `owner: "telegram:<id>"`,
   enable the channel, restart.

### Discord

1. https://discord.com/developers → New Application → Bot → copy token,
   enable the **Message Content** privileged intent.
2. OAuth2 → URL Generator: scopes `bot`, permissions *Send Messages*,
   *Read Message History* → open the URL to invite it (or just DM it).
3. `export DISCORD_BOT_TOKEN=...`, allowlist your user id
   (Discord settings → Advanced → Developer Mode → right-click → Copy ID).
4. Set `owner: "discord:<user id>"`, enable, restart.

### WebChat (built-in)

Enabled by default on `http://127.0.0.1:18790/`, bound to localhost
only. The shipped defaults are default-deny like every other channel
(`allowlist: []`, `owner_peer: ""`): binding to 127.0.0.1 stops other
*machines*, but any local process — or any web page open in your
browser — can reach the port and claim any peer id, so a wildcard
allowlist would let it drive the agent and answer its own approval
requests. Set `allowlist`/`owner_peer` to the peer id you use before
chatting. Change the port with `agentkai gateway --port 8080`.

### WhatsApp (experimental bridge)

agentkai has no native WhatsApp support — the web-client libraries are
Node-only — so this channel talks to a small sidecar you run yourself:

1. `cd src/agentkai/channels/examples/whatsapp-sidecar && npm install`
2. `node server.js` → scan the printed QR with WhatsApp (Linked devices).
3. Allowlist your number (`+15551234567` format), set the owner peer,
   enable `whatsapp`, restart the gateway.

The sidecar keeps no chat history beyond an in-memory inbox; restarting it
is safe. Experimental: WhatsApp may rate-limit automated web clients —
use a spare number.

## Limits

- Text only (no images/voice in v1); non-text Telegram/Discord messages are
  ignored.
- Long replies are split per channel (Telegram 4096, Discord 2000 chars).
- Gateway runs are independent of the dashboard/CLI; they share the same
  `~/.agentkai` files (memory, config) but not live session state.
