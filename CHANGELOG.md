# Changelog

All notable changes to agentkai are documented here. Versioning follows
[SemVer](https://semver.org/): MINOR for features, PATCH for fixes.

## [0.5.0] - 2026-10-07

### Added
- `agentkai runs list` / `agentkai runs show RUN_ID`: inspect past runs
  from the terminal, without opening the dashboard. `list` shows
  newest-first run summaries (status, model, tools used) with `--limit`
  and `--json`; `show` replays one run's `events.jsonl` in order
  (run start, LLM messages, tool calls/results, approvals, run end),
  also with `--json`. Both honor `$AGENTKAI_HOME` like the rest of the
  CLI. Until now the event log was only reachable through the dashboard
  API or by reading the JSONL files by hand, even though
  `events.list_runs`/`summarize`/`replay` have been public API since
  0.1.0. Six new tests in `tests/test_runs_cli.py` seed real event logs
  and cover empty state, ordering, `--limit`, both `--json` modes, and
  the unknown-run error (exit 1).

## [0.4.1] - 2026-10-06

### Fixed
- `LLMClient.generate` now retries the **whole** fallback chain, not just
  the first fallback. Previously the retry loop bailed out after the first
  fallback failed (`attempt == 0` only), so a request could raise
  "all models failed" without ever trying the remaining models in the
  chain — contradicting the documented fallback behavior in
  `docs/models.md`. The final error now also names every model that was
  actually tried. Three regression tests in `tests/test_core.py` cover the
  full-chain retry, the exhausted-chain error, and the no-fallback fast
  path.

## [0.4.0] - 2026-10-05

### Added
- `agentkai doctor`: read-only environment diagnostics. Checks the agent
  home, `config.yaml` (fails on unparseable YAML — the config loader
  itself silently ignores it), provider API-key status for every model
  alias (warns when the default model's key is missing), the scheduler
  SQLite database (job counts plus failures in the last 24h; fails on a
  corrupt/unreadable DB), the memory tree, the built-in tool registry
  (fails on unknown risk levels), image media providers (OpenAI key,
  local Stable Diffusion server, or an honest warning), and the bundled
  dashboard static assets (fails on missing `index.html`, which would
  mean broken packaging). Prints `[OK]`/`[WARN]`/`[FAIL]` per check,
  exits 1 on any failure; `--json` for a machine-readable report.
  13 new tests in `tests/test_doctor.py`; README CLI table,
  `docs/quickstart.md`, and `docs/troubleshooting.md` document it.

## [0.3.0] - 2026-10-04

### Added
- `PUT /api/memory/{name}` (closes the API.md roadmap item): edit any
  memory file from the dashboard or API — body `{content}`, same
  root-level name rules as the GET (400 on path traversal), 1 MiB cap.
  The previous version is kept as a `<name>.bak` sidecar via atomic
  rename; response is `{name, created, backup, bytes}` (201 on create,
  200 on update). The dashboard Memory page gained an Edit/Save UI that
  surfaces the backup filename.
- `GET /api/tools` (closes the API.md roadmap item): lists the built-in
  tool registry as `{tools: [{name, description, risk}]}` via a new
  `Registry.describe()` accessor (MCP-attached remote tools are per-run
  and intentionally not listed). The dashboard has a matching Tools page
  with per-tool risk pills.
- `Memory.replace(name, text) -> (created, backup)`: atomic replace with
  `.bak` sidecar; backs the PUT endpoint.
- `create_app()` accepts an injectable `Memory` (tests back new
  endpoints with a temp memory root).

### Changed
- `src/agentkai/dashboard/API.md`: memory editing and the tool registry
  moved from "Roadmap" into the documented endpoint contract; roadmap
  is empty.
- `docs/dashboard.md`: corrected the stale "stubbed run simulation"
  note (the chat page drives the real agent loop) and documented the
  new pages.

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
