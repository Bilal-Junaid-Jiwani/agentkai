# Companion-app contract (devices)

AgentKai talks to your phone/laptop **only through the AgentKai companion
app** — a small app you install on the device. There is no cloud relay and
no fake built-in SMS sender: if the app isn't paired, the device tools
return an honest error telling you exactly that.

## Pairing

1. On your computer: `agentkai devices pair` → prints a 6-character code
   (valid 10 minutes).
2. In the app: enter the code. The app then calls the dashboard:

```
POST http://<your-computer>:<port>/api/devices/claim?token=<dashboard-token>
Content-Type: application/json

{
  "code": "KQ7M2X",
  "device": {
    "name": "pixel-8",
    "device_type": "phone",
    "api_base": "https://192.168.1.42:8443",
    "capabilities": {"sms": true, "contacts": true, "calendar": true, "push": true}
  }
}
```

Response: `{"device_id": 1, "token": "<bearer-token>", ...}` — the app
stores the bearer token in the device keychain/keystore. AgentKai stores
only its SHA-256 hash.

> The dashboard binds `127.0.0.1` only, so the app cannot reach
> `/api/devices/claim` directly from another machine. Complete pairing
> through a tunnel you control (e.g. `ssh -L`), or run a pairing relay
> yourself. AgentKai will never ship a cloud relay — your traffic stays
> yours.

## App endpoints (what the app must implement)

All calls use `Authorization: Bearer <token>` (the token from pairing).
All paths are under the app's own `api_base`.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/agentkai/v1/sms` | `{"to": "+92...", "body": "..."}` | `{"message_id": "..."}` |
| GET | `/agentkai/v1/contacts` | `?q=bilal` (optional) | `{"contacts": [{"name","phones":[],"emails":[]}]}` |
| GET | `/agentkai/v1/calendar` | `?days=7` (1–60) | `{"events": [{"title","start","end","location"}]}` |
| GET | `/agentkai/v1/notifications` | — | `{"notifications": [{"id","title","body","created_at"}]}` |
| POST | `/agentkai/v1/notifications/ack` | `{"ids": [1,2]}` | `{"ok": true}` |

The agent queues notifications with the `device_notify` tool; the app polls
`GET /notifications` (or receives them via the push channel it registered)
and acks with the second endpoint. SMS sending must go through the device's
native SMS stack — never through a third-party SMS gateway account owned
by AgentKai, because there isn't one.

## Security notes

- Tokens are random 256-bit values, stored hashed (SHA-256). Revoke with
  `agentkai devices revoke <id>` — the hash row is deleted, so the old
  token stops working immediately.
- Pairing codes are single-use and expire after 10 minutes.
- The agent → app channel is only as secure as the network you run the
  app's `api_base` on: use HTTPS with a certificate you trust, preferably
  on your own LAN/VPN.
