# Changelog

All notable changes to agentkai are documented here. Versioning follows
[SemVer](https://semver.org/): MINOR for features, PATCH for fixes.

## [0.6.0] - 2026-10-14

### Added
- **Browser request gate: the non-public-host SSRF boundary now applies
  to every request Chromium makes, not just the initial navigation.**
  0.5.6 closed the navigation-time gap and documented the remaining
  limitation honestly — redirect hops, subresources, XHR/fetch, and
  websockets fired by a loaded page were followed by Chromium itself with
  no re-validation, so a prompt-injected page could have steered the
  browser at `http://169.254.169.254/latest/meta-data/`, `127.0.0.1`, or
  `192.168.x.x` and read the response into the page (and into the
  agent's context). `BrowserSession` now registers a context-wide
  Playwright route handler (`**/*`, covering all tabs including ones
  opened later) that enforces the same `_assert_public_host()` boundary
  as `fetch_url` hop-by-hop: requests to non-public or unresolvable
  hosts are aborted, as are `file://` requests (a page can never read
  the local disk, even via iframe/subresource). Harmless schemes
  (`data:`, `blob:`, `about:`, `chrome:`, ...) pass through untouched,
  and the handler fails closed — an unexpected error aborts the
  request rather than letting it through. Eleven new tests in
  `tests/test_browser.py` cover registration, the public-host allow
  path (http/https and wss), and aborts for link-local metadata IP,
  loopback, private, and unresolvable hosts, `file://` URLs, ws-to-loopback,
  harmless schemes, and the fail-closed path. Playwright's route handler
  fires per redirect hop, so this supersedes the 0.5.6 known-limitation
  note.

## [0.5.6] - 2026-10-13

### Fixed
- **Browser navigation now refuses non-public hosts (SSRF gap closed).**
  `fetch_url` has long refused hosts that resolve to loopback, private,
  link-local, or otherwise non-public addresses, but the real-Chromium
  browser tool's URL validation only checked the scheme and rejected
  credentials-in-URL — so `browser_goto http://169.254.169.254/...`
  (the cloud metadata endpoint), `http://127.0.0.1:8501/`, or
  `http://192.168.1.10/...` loaded happily (verified with a repro:
  all three passed validation while `fetch_url` refused them). A
  prompt-injected page could have steered the browser at internal
  services or metadata URLs and the response would have landed in the
  agent context. The non-public-host check is now extracted from
  `tools._validate_url` into a shared `_assert_public_host()` helper and
  enforced by `browser._validate_url`, so `browser_goto` (and `new_tab`
  with a URL, which routes through the same gate) refuses unresolvable
  hosts and every non-public address class with the same messages as
  `fetch_url`. Six new tests in `tests/test_browser.py` cover private,
  loopback, and link-local (metadata IP) refusals, unresolvable hosts,
  and the public-host allow path; the suite's DNS is now fully mocked
  so browser navigation tests never touch real DNS. Known limitation,
  unchanged: redirects *within* an already-loaded page are followed by
  Chromium itself and are not re-validated hop-by-hop the way
  `fetch_url` does.

## [0.5.5] - 2026-10-12

### Fixed
- **`--pin` on skill installs now actually checks out the pinned commit.**
  The 0.5.2 supply-chain hardening added `--pin`/`pin` to pin a remote
  skill install to a commit SHA, but the implementation only compared
  `git clone --depth 1`'s HEAD against the pin afterwards and never
  checked the pin out — so pinning to any non-HEAD commit always failed
  with "pin mismatch" (verified with a repro against a real 3-commit
  git repo), and the feature's whole point (install the exact commit
  you reviewed) was silently broken. `install_skill()` now fetches the
  pinned commit and `git checkout --detach`s it before recording
  `commit_sha` in the manifest; the pin must be a full 40-hex SHA
  (required for fetch-by-SHA, and the strict shape also rules out
  option injection through the pin value), and an unfetchable commit
  fails the install with the half-cloned directory cleaned up, as
  before. Four new tests in `tests/test_skills.py` cover pinning to an
  older commit (checked-out tree, manifest SHA, and clean load),
  pinning to HEAD, invalid pin formats, and unfetchable commits.

## [0.5.4] - 2026-10-11

### Fixed
- **Media `out_path` can no longer escape the media directory.** The
  `generate_image`, `generate_video`, and `edit_image` tools joined a
  relative `out_path` to `~/.agentkai/media/` but never checked the
  result, so a path like `../../pwned.png` wrote the generated file
  *outside* the media dir (verified with a repro: a stub provider wrote
  three files outside the dir). The traversal guard in `generate_image`
  was dead code — after joining, the path was always absolute, so the
  check could never fire — and the other two functions had no guard at
  all, quietly bypassing the filesystem sandbox the file/shell tools
  enforce. All three now resolve the path through one helper that
  collapses `..` segments and symlinks and refuses anything landing
  outside the media dir; relative subdirectories (e.g.
  `campaigns/hero.png`) still work and get their parents created, and
  absolute paths are accepted only inside the media dir. Seven new tests
  in `tests/test_media.py` cover `../` traversal, absolute-path escape,
  symlink smuggling, and the legitimate cases.

## [0.5.3] - 2026-10-10

### Fixed
- **Skill manifest verification now fails closed.** The 0.5.2 integrity
  check silently skipped verification whenever an installed user skill's
  `manifest.json` was unreadable, invalid JSON, or missing its
  `content_sha256` — so corrupting the manifest switched the drift
  check off and let tampered `tools.py` code execute (verified with a
  repro: a marker-dropping `tools.py` ran despite a destroyed
  manifest). `SkillLoader.load()` now refuses such skills with a
  `SkillError` before any skill code runs, and `agentkai skills list`
  flags the broken manifest instead of showing the skill as healthy.
  Skills with no manifest at all (manually copied directories) keep
  loading as before. Note: skills installed by agentkai ≤ 0.5.1 carry
  a manifest without an integrity hash and must be reinstalled once
  (`agentkai skills install <source> --force`) to stamp one. Five new
  tests in `tests/test_skills.py` cover the corrupt, hash-less, and
  non-object manifest refusals (tampered code never executes), the
  manifest-less manual-copy path, and the `skills list` flagging.

## [0.5.2] - 2026-10-09

### Fixed
- **Skill installer supply-chain hardening.** Installing a third-party
  skill previously cloned any git URL with no commit pin or integrity
  hash, and the skill's `tools.py` then executed unsandboxed inside the
  agentkai process on load — so a tampered or swapped repo could run
  arbitrary code. Installs now record `commit_sha` (git installs) and a
  `content_sha256` (SHA-256 over sorted relative paths + file bytes,
  excluding `manifest.json`) in `manifest.json`; `SkillLoader.load()`
  recomputes the hash for `source == "user"` skills and raises
  `SkillError` on drift *before* `tools.py` executes. Remote/git
  installs additionally require explicit trust (`install_skill(...,
  trust_remote=True)`, CLI `--trust-remote`) with the resolved URL
  shown, and support `--pin <commit>` to pin a clone to a commit.
  Local-directory installs are unchanged; bundled skills are
  unaffected. Skills without a manifest (manually copied dirs) keep
  loading as before. No sandbox is introduced — this closes the
  silent-tamper hole, not the trust decision itself. Four new tests in
  `tests/test_skills.py` cover the recorded hash, drift refusal
  (tampered module-level code never executes), the trust gate, and pin
  mismatch cleanup.

## [0.5.1] - 2026-10-08

### Fixed
- The `exec` tool now kills the whole process group when a command times
  out, as its own `start_new_session` comment always intended.
  Previously `subprocess.run` killed only the shell itself, so anything
  the command had backgrounded (e.g. `sleep 60 & wait`) silently
  survived the timeout and kept running on the user's machine. The
  timeout path now SIGKILLs the process group and reaps it, returning
  the same `{exit_code: -1, "ERROR: timed out …"}` result as before.
  Regression test in `tests/test_tools.py` backgrounds a child, records
  its PID, and asserts it is gone after the timeout.

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
