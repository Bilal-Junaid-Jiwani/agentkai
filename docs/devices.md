# Devices

Pair your phone or laptop so the agent can reach it: push notifications,
SMS via the device's own stack, contacts, and calendar. AgentKai never
talks to a phone directly — it talks to the **AgentKai companion app**
over plain HTTPS. The app is the thing that can touch SMS and contacts;
the agent just asks it.

There is no cloud relay and no fake built-in SMS sender. If no device is
paired, the device tools return an honest error saying exactly that.

## Pairing

1. On your computer: `agentkai devices pair` → prints a 6-character code
   (single-use, valid 10 minutes).
2. In the companion app: enter the code. The app calls back to claim the
   pairing and receives a Bearer <redacted>, which it stores in the device
   keychain/keystore. AgentKai stores only the token's SHA-256 hash.

```bash
agentkai devices list              # paired devices + capabilities
agentkai devices notify "Hi" "Test" # queue a push (manual test)
agentkai devices revoke 1          # token stops working immediately
```

> Pairing caveat: the claim endpoint lives on the dashboard, which binds
> `127.0.0.1` only — your phone can't reach it directly. Complete pairing
> through a tunnel you control (e.g. `ssh -L`), or run your own pairing
> relay. AgentKai will never ship a cloud relay; your traffic stays yours.

## What the agent can do with a paired device

Via the `device_notify`, SMS, contacts, and calendar tools: queue a push
notification (the app polls and delivers), send an SMS **through the
device's native SMS stack** (never a third-party gateway), look up
contacts, and read the device calendar. All of these go through the
normal permission gate — sending an SMS asks first.

## The app contract

The companion app implements a small HTTP API under its own `api_base`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/agentkai/v1/sms` | Send SMS (`{"to", "body"}`) |
| GET | `/agentkai/v1/contacts` | Search contacts (`?q=`) |
| GET | `/agentkai/v1/calendar` | Upcoming events (`?days=`) |
| GET | `/agentkai/v1/notifications` | Poll queued notifications |
| POST | `/agentkai/v1/notifications/ack` | Acknowledge delivered |

All calls use `Authorization: Bearer <token>`. Full contract in
`src/agentkai/devices/README.md`.

## Security notes

- Tokens are random 256-bit values, stored hashed. Revocation deletes the
  hash — the old token stops working immediately.
- The agent→app channel is only as secure as your network: use HTTPS with
  a certificate you trust, preferably your own LAN/VPN.
- No companion app exists yet in this repo — the contract above is what
  an app must implement. Until one does, the tools honestly report
  "no paired device".
