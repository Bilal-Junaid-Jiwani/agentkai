# Dashboard

Local web dashboard (Phase 7).

- FastAPI on `127.0.0.1` only — never exposed to the network
- Per-launch token auth
- Offline single-page app (no CDN dependencies)
- Live view of the agent event log over SSE: what it's doing right now,
  tool calls, scheduler runs, memory

Same pattern as proven local dashboards: localhost-only + token = your data
never leaves the PC.
