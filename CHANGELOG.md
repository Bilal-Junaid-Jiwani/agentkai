# Changelog

All notable changes to agentkai are documented here. Versioning follows
[SemVer](https://semver.org/): MINOR for features, PATCH for fixes.

## [0.6.8] - 2026-10-23

### Fixed
- **Exec blocklist: dot-segment path spellings can no longer bypass it.**
  The blocklist matched ``rm`` targets against literal spellings of ``/``
  and the home directory, but the shell resolves dot segments before
  acting — so ``rm -rf /./`` and ``rm -rf /../`` (and the same spellings
  behind ``sudo``, nested ``bash -c '...'`` payloads, and ``find`` search
  roots) slipped straight through while doing exactly what ``rm -rf /``
  does. ``_is_root_or_home`` now normalizes each target the way the shell
  resolves it (``~``/``$HOME``/``${HOME}`` expanded, ``//``/``/./``/``/../``
  collapsed via POSIX normalization) before comparing, so the obfuscated
  spellings are caught too — without flagging near-misses like
  ``rm -rf $HOME/../shared`` (the parent of home, not home). New
  regression tests pin both the blocked spellings and the allowed ones.

## [0.6.7] - 2026-10-22

### Fixed
- **Browser tools are now thread-affine.** `_run_tool_guarded` ran every
  tool call on a fresh worker thread, but the persistent `BrowserSession`
  drives Playwright's sync API, which must be used from the thread that
  created it — so browser calls could fail or deadlock when a new tool
  call landed on a different thread than the one that started Chromium.
  `Tool` gained a `thread_affinity` flag; all browser tools set it, and
  the agent loop routes those calls through a dedicated per-tool worker
  thread instead of a fresh thread per call.
- **MCP SSE transport rejects cross-origin endpoints.** The transport
  took its POST endpoint from the server's `endpoint` event via
  `urljoin` with no origin check, so a compromised MCP server could
  redirect all JSON-RPC traffic (tool names, arguments, results) to an
  arbitrary host. The announced endpoint must now share scheme, host,
  and port with the SSE stream URL; a mismatch fails closed with
  `MCPError`.
- **Context compaction summary is no longer `role: "user"`.** Phase-2
  budget compaction inserted a summary of *untrusted tool output* as a
  user message — a prompt-injection foothold, since the model treats
  user-role content as instructions. The summary is now `role: "system"`
  and explicitly labeled as compacted tool output, never new user
  instructions.

### Verified and refuted (Gemini Argon audit claims checked in source)
- Skill manifest "bypass": refuted — `_verify_user_skill` is fail-closed
  (unreadable/invalid/non-object manifest or missing `content_sha256`
  refuses the load) and runs on every user-skill load.
- Dashboard token issue: refuted — per-launch random token, localhost-only
  bind, token-gated middleware with constant-time comparison, never
  logged, printed exactly once at startup.
- Missing dependency floors: refuted — every runtime dependency in
  `pyproject.toml` carries a `>=` floor.
- Approval cancellation/skip: refuted — approvals fail closed: timeout
  denies, unknown run returns 404, `action_id` mismatch returns 400; no
  cancel-to-allow path exists.

## [0.6.6] - 2026-10-22

### Fixed
- **Critical: `agentkai run` works again on a fresh install.** Releases
  through 0.6.5 declared `litellm>=1.60` with no upper bound and never
  declared `mcp`, but LiteLLM's tool-calling path imports `mcp.types`
  at call time — so a clean `pip install agentkai` produced an
  install where every run died with "all models failed ... No module
  named 'mcp'". `mcp>=2.2,<3` is now a declared dependency (matching
  LiteLLM's own requirement for that code path) and LiteLLM is capped
  at `<2.0`. `agentkai doctor` gained a `litellm` check that probes
  exactly these imports, so a partial install is diagnosable up front
  instead of at first run. New `scripts/smoke_install.py` installs the
  built wheel into a clean venv and proves the run path reaches the
  model stage — the class of check that would have caught this.
- **Fallback-chain errors tell the truth.** When every model in a chain
  failed, only the LAST provider's error was surfaced — a bad Anthropic
  key on the primary showed up as "OpenAI credentials missing" from the
  tail of the chain. The error now lists each model's own failure in
  chain order (primary first), each collapsed to one line.
- **`exec` blocklist hardening (still a blocklist, not a sandbox).**
  The destructive-command scan now matches `rm` by basename (`/bin/rm
  -rf /` slipped past), treats `$HOME`/`${HOME}` like `~`, recurses
  into nested `sh -c '...'` / `eval` payloads, and blocks
  `find / -delete` and download-piped-to-shell one-liners
  (`curl ... | sh`). Docs and the tool description now state plainly
  that the command itself is not sandboxed, the permission gate is the
  real protection, and a container/VM is the right home for untrusted
  or unattended use. Regression tests cover every named bypass plus
  near-miss commands that must stay allowed (`rm -rf ./build`,
  `curl ... | sha256sum`).
- **Scheduler refuses garbage schedules at add time.** `Scheduler.add`
  never validated, and the CLI path didn't either — a job with schedule
  `"not a cron"` was happily stored and could never fire. `add()` now
  validates cron and `@at:` schedules (the dashboard API already did),
  and the CLI reports a clean `bad schedule ...` error with exit 2.
- **Docs caught up with the code.** README and the docs index still
  called the software "v0.3.0"; both now say v0.6.6, and the doctor /
  scheduler / models / tools pages describe the checks, validation,
  error format, and exec safety model as they actually are.

## [0.6.5] - 2026-10-21

### Fixed
- **Security: `transcribe_audio` input files are now confined to the
  agent workspace.** The tool accepted an unconstrained `audio_path`,
  so a model could point it at any audio file on the host (e.g. another
  application's recordings) and have the transcript fed back to it. Input
  paths are now confined exactly like the file/shell and media tools:
  relative paths resolve against `<home>/workspace`, absolute paths must
  already live inside it, and `..` traversal or symlink escapes are
  refused with a clear error (surfaced as `ERROR: ...` by the tool).
  The gateway hook `preprocess_voice_note` is unchanged: channels
  download voice notes themselves and call it on the downloaded file.
  Eight new tests in `tests/test_voice.py` cover absolute-path,
  traversal, tilde, and symlink escapes, inside-workspace acceptance
  (absolute and relative), and missing-file rejection.

## [0.6.4] - 2026-10-20

### Fixed
- **Security: `speak_text` output paths are now confined to the agent's
  voice directory.** The low-risk `speak_text` tool accepted an
  unconstrained `out_path`, so a model could point it at any
  process-writable file (e.g. `~/.bashrc`) and have it overwritten with
  synthesized audio bytes. Output paths are now confined exactly like
  the media tools: relative paths resolve against `<home>/voice`,
  absolute paths must already live inside it, and `..` traversal or
  symlink escapes are refused with a clear error (surfaced as
  `ERROR: ...` by the tool). The default output location is unchanged
  (`<home>/voice/tts-<stamp>-<id>.wav`). Seven new tests in
  `tests/test_voice.py` cover absolute-path, traversal, tilde, and
  symlink escapes plus inside-voice-dir acceptance and the default path.

## [0.6.3] - 2026-10-17

### Fixed
- **Security: `edit_image` input files are now confined to the agent
  workspace.** The `image` and `mask` arguments were resolved against the
  raw filesystem, so a model could point the tool at an arbitrary file
  (e.g. `~/.ssh/id_rsa`, `/etc/passwd`) and have its bytes exfiltrated to
  the remote image-editing provider. Input paths are now confined exactly
  like the file/shell tools: relative paths resolve against
  `home/workspace`, absolute paths must already live inside it, and `..`
  traversal or symlink escapes are refused with a clear error. Six new
  tests in `tests/test_media.py` cover absolute-path, traversal, symlink,
  and mask escapes plus inside-workspace acceptance.

## [0.6.2] - 2026-10-16

### Fixed
- **Permission gate: the in-memory audit log no longer grows without
  bound.** `PermissionGate._audit` appended one entry per tool decision
  for the whole process lifetime — and gateway agents (plus their gates)
  live as long as the gateway runs, so a long-lived gateway leaked memory
  on every tool call (verified: 50,000 decisions → 50,000 in-memory
  entries). The in-memory `audit_log()` now keeps the most recent
  `AUDIT_CAP = 2000` decisions (oldest-first eviction, same window as the
  WhatsApp bridge's seen-id cache); when `audit_path` is configured, the
  JSONL file remains the durable, complete record of every decision. Two
  new tests in `tests/test_core.py` verify the bound/eviction and that the
  JSONL file still records everything.

## [0.6.1] - 2026-10-15

### Fixed
- **WhatsApp bridge: the seen-message-id dedup cache no longer grows
  without bound.** `WhatsAppBridge._seen` collected every sidecar event id
  it ever dispatched, so a gateway left running for weeks would keep the
  full history in memory forever. The cache is now capped at
  `SEEN_CAP = 5000` ids with oldest-first eviction. Eviction is safe
  because the sidecar only returns events at or after the `since`
  watermark, so a re-sent evicted id simply gets re-dispatched — the
  correct fallback. A new test in `tests/test_channels.py` verifies the
  bound, oldest-first eviction, and that within-window dedup still works.

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
