# agentkai dashboard — API contract

Version: 0.1.0 (dashboard v1). The chat/run endpoints drive the **real**
agent loop (`agentkai.agent.Agent`); every event below comes from a live
run, never a simulation.

## Auth

Every request carrying data (the `/` page, all `/api/*`, SSE) requires the
per-launch token, issued once at startup:

- `GET /?token=<token>` — query param (required for `EventSource`, which
  cannot set headers), or
- `Authorization: Bearer <token>` header.

The static UI shell (`/static/*` — css/js/img, no data) is served without a
token so the page can bootstrap; the server is localhost-only regardless.
Missing/invalid token → `401 {"detail": "invalid or missing token"}`.
The token is printed exactly once by `agentkai dashboard` and never logged
(uvicorn access log disabled).

## Transport

- Base: `http://127.0.0.1:<port>` — the server refuses any other bind host.
- `GET /` → the SPA shell (`index.html`); `/static/*` → vendored assets.
  No external (CDN) requests, ever.
- JSON everywhere except SSE.

## Event model

Every chat run produces an append-only event projection. Event shape:

```json
{ "seq": 12, "ts": 1730000000.0, "run_id": "9f2c…", "type": "message_delta",
  "data": { "role": "assistant", "text": "Working on it — " } }
```

The durable source of truth is the agent's own log at
`~/.agentkai/runs/<agent_run_id>/events.jsonl`; the dashboard store is a
live projection of it (the dashboard `run_id` and the agent's run id are
linked via the run record's `agent_run_id` field).

Event types: `run_started` (`{prompt, model}` — resolved LiteLLM model
string), `message_delta` (`{role, text}` — streaming chunks, concatenate in
order), `message` (`{role, text}` — complete message; emitted only when the
provider didn't stream), `tool_call` (`{tool, args, risk, call_id}` —
streamed live the moment the agent records the call, before the tool
executes), `tool_result` (`{tool, ok, output}` — streamed live when the
tool finishes), `approval` (`{tool, args, risk,
decision, action_id}` — `decision` is `pending` while the approval card
waits, then `allow`/`deny`), `run_finished` (`{status}` — `done` or
`error`), `error` (`{message}` — honest failure, never a faked reply).

Approval flow: when the permission gate decides `ask`, the run pauses and an
`approval` event with `decision: "pending"` is streamed. The chat UI renders
an Approve/Deny card; clicking resolves it via `POST /api/runs/{id}/approve`.
Approvals time out after 10 minutes (denied). Unattended runs should use a
model whose provider key is configured; if no key is configured for the
chosen model, the run ends immediately with an `error` event explaining how
to configure it — nothing is faked.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | `{ok, version, uptime_s, model_default, runs_active}` |
| GET | `/api/models` | `{default, models: [{alias, resolved}], key_status: {provider: "set"\|"missing"}}` — model aliases, resolved LiteLLM strings, and BYOK key presence (never values) |
| GET | `/api/runs` | `{runs: [{id, prompt, model, status, created_at, finished_at, agent_run_id?}]}` — newest first. `status`: `running`/`done`/`error` |
| GET | `/api/runs/{id}` | one run record, 404 if unknown |
| GET | `/api/runs/{id}/events` | `{run_id, events: [...]}` — full replay, 404 if unknown |
| POST | `/api/chat` | body `{prompt, model?}` → `{run_id}`. Starts a real agent run; stream it via SSE below. 400 on empty prompt |
| POST | `/api/runs/{id}/approve` | body `{action_id, approved}` → `{ok: true}`. Resolves a pending permission-gate approval. 404 if none pending, 400 on `action_id` mismatch |
| GET | `/api/events?run_id={id}` | `text/event-stream`: replays the run's stored events, then streams new ones live until `run_finished`, then closes. 404 on unknown run |
| GET | `/api/events` | `text/event-stream`: live activity feed across all runs (no replay), `: ping` keepalives |
| GET | `/api/memory` | `{root, files: [{name, exists}]}` — SOUL/USER/MEMORY/AGENTS/IDENTITY/TOOLS.md + daily logs (wired to the real `Memory` class) |
| GET | `/api/memory/{name}` | `{name, content}` — 400 on path traversal, 404 if missing |
| GET | `/api/scheduler/jobs` | `{db, jobs: [{name, schedule, command, prompt, model_alias, job_type, enabled, last_run}]}` — wired to the real `Scheduler` SQLite store |
| POST | `/api/scheduler/jobs` | body `{name, schedule, command?, prompt?, model_alias?, job_type?, enabled?}` → `{ok, name, created, job}`. Creates or updates a job; `schedule` is a 5-field cron or `@at:<ISO>` for one-shots (type `at`). `command` or `prompt` required for `cron`/`at` types. 400 on invalid schedule/type/payload; 201 on create, 200 on update |
| DELETE | `/api/scheduler/jobs/{name}` | → `{ok, name}`. 404 if unknown |
| POST | `/api/scheduler/jobs/{name}/run` | Runs the job now regardless of schedule → `{name, status, summary, started_ts, finished_ts}`. 404 if unknown, 400 if the job is disabled |

## Roadmap (contract additions, not changes)

- `PUT /api/memory/{name}` — edit memory files (with backup)
- `GET /api/tools` — tool registry listing
