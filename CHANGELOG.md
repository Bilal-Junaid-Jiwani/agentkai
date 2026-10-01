# Changelog

All notable changes to agentkai are documented here. Versioning follows
[SemVer](https://semver.org/): MINOR for features, PATCH for fixes.

## [0.2.0] - 2026-10-01

### Added
- Dashboard scheduler job management (the `API.md` roadmap item):
  - `POST /api/scheduler/jobs` — create or update a job (201 on create,
    200 on update). Validates the schedule (5-field cron, or `@at:<ISO>`
    for one-shot jobs) and requires a `prompt` or `command` for
    `cron`/`at` job types, mirroring `agentkai scheduler add`.
  - `DELETE /api/scheduler/jobs/{name}` — remove a job (404 if unknown).
  - `POST /api/scheduler/jobs/{name}/run` — run a job now regardless of
    schedule (404 if unknown, 400 if disabled); outcome is recorded in
    the job's run history.
  - The dashboard Scheduler page got a matching UI: an add-job form plus
    per-row "Run now" / "Delete" actions.
  - `GET /api/scheduler/jobs` now also returns `prompt`, `model_alias`,
    `job_type` and `last_run` for each job.
- `create_app()` accepts an injectable `Scheduler` (tests back new
  endpoints with a temp SQLite store).

### Changed
- `src/agentkai/dashboard/API.md`: scheduler job endpoints moved from
  "Roadmap" into the documented endpoint contract; roadmap now lists
  memory editing and the tool registry.
- `docs/dashboard.md`: notes scheduler job management as shipped in v0.2.0.

## [0.1.0] - 2026-09-29

First public release.

- ReAct agent loop, multi-provider model layer (Claude/Gemini/GPT/Ollama/
  GLM via LiteLLM), safe shell/fs/web tools, MCP client, markdown
  memory, subagents, SQLite scheduler, messaging gateway
  (Telegram/Discord/WebChat/WhatsApp), real Chromium browser tool,
  12 bundled skills, voice STT/TTS, media generation, devices, goals,
  widgets, and a token-secured localhost dashboard.
- 357 tests green (4 skipped); real-Chromium verification 13/13.
