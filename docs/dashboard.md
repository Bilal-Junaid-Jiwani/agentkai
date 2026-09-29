# Dashboard

The local web dashboard: chat with the agent, watch runs stream,
replay past runs event-by-event, browse memory, and inspect scheduler
jobs — all in the browser, all offline.

```bash
agentkai dashboard            # default http://127.0.0.1:8931/
agentkai dashboard --port 9000
```

## Security posture

- Binds **127.0.0.1 only** — passing any other host is refused. It is not
  reachable from your LAN or the internet, by design.
- A **fresh random token** is generated per launch, printed once by the
  CLI, and required on every data request (query param `?token=` or
  `Authorization: Bearer` header). Wrong token → 401. The token is never
  logged (uvicorn access log is disabled).
- The SPA is fully **offline**: no CDN, no external fonts, no analytics.
  The only network the page touches is the dashboard itself.

## Pages

- **Chat** — send a prompt, watch the answer stream token-by-token over
  SSE, with tool calls shown inline as they happen.
- **Runs** — every run's history with a full event timeline
  (messages, tool calls, results, approvals). Replay any finished run.
- **Memory** — browse SOUL/USER/MEMORY.md and daily logs.
- **Scheduler** — inspect jobs from the real scheduler database.

## Under the hood

The backend is FastAPI; the frontend is a small dependency-free SPA in
`src/agentkai/dashboard/static/`. Runs emit an append-only event log
(`~/.agentkai/runs/<run_id>/events.jsonl`); the dashboard replays and
live-streams those events over Server-Sent Events.

Honest v1 note: the chat page currently streams from a stubbed run
simulation while the real agent-core wiring lands — the API contract
(`src/agentkai/dashboard/API.md`) is stable and won't change when the
real loop is plugged in. Scheduler and memory pages already read the
real stores.

## API

The full endpoint contract is documented in
`src/agentkai/dashboard/API.md` (installed with the package): auth,
event shapes, and every `/api/*` endpoint, plus the planned additions
(job CRUD, memory editing, approval cards).
