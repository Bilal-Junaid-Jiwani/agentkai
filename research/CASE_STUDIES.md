# open-agent — Case Studies: 10 real agent projects

Date: 2026-09-29. Every project below is real and was verified during this
research pass. URLs are given only where seen verbatim in research sources;
where a URL could not be verified it is marked as such rather than guessed.
For each: what it is, the architecture pattern it uses, and what open-agent
should **borrow** or **avoid**.

---

## 1. OpenHands (formerly OpenDevin)
- URL: https://github.com/All-Hands-AI/OpenHands (Apache-2.0)
- What: autonomous software-development agent runtime + SDK (v1 is a modular
  Python SDK with four packages).
- Architecture:
  - **Event-sourced loop.** `AgentController` drives the loop; every user
    message, action, observation, and state change is a typed event appended to
    an immutable `EventStream`. State is a projection of the log. Gives
    pause/resume, replay, fork, and remote observation from one substrate.
  - **Pluggable agents:** `CodeActAgent` (default), browsing agents, etc.
  - **Runtime (sandbox):** Docker per session; actions execute over REST in an
    isolated container — host never exposed to agent code.
  - **Skills/MCP:** `AgentContext` centralizes system-prompt inputs; skills
    load from markdown files (`.openhands/skills/`, `.cursorrules`-compatible);
    skills may bundle MCP tools. ACP (Agent Communication Protocol) lets the
    loop delegate cognition to external agents (e.g. `claude-agent-acp`).
  - **Memory:** `openhands/memory/` with condensers for long conversations.
- **Borrow:** the append-only event log as the single source of truth; the
  strict separation of *agent (reasons)* from *runtime (executes)*; skills as
  markdown + MCP tools.
- **Avoid:** the heavyweight Docker-first execution model for v1 — right for a
  coding agent, overkill for a personal assistant. Also note its complexity
  budget: leases, generation fencing, multi-runtime backends. We take the
  event-log idea, not the distributed-systems machinery.

## 2. OpenClaw (formerly Clawdbot / Moltbot)
- URL: GitHub org not verified in this pass (docs seen at
  https://docs.openclaw.ai/reference/templates/SOUL); widely described as MIT
  open source. Install via `@openclaw/cli`.
- What: personal AI agent ("AI chief of staff") that lives on a VPS or
  always-on machine and talks to you over WhatsApp/Telegram.
- Architecture:
  - **Gateway:** always-on persistent service connecting the model to tools,
    sessions, skills, and memory.
  - **SOUL.md:** the single identity/personality file — role, tone, hard
    limits. Without it the agent has no grounding.
  - **ReAct engine:** reason → act → observe loop.
  - **Memory:** local markdown files + vector DB; daily notes; `MEMORY.md`.
  - **Heartbeat:** configurable interval (5–30 min) proactive wake — the
    autonomy engine behind all cron automations.
  - **Cron store:** JSON job file (users keep it in the workspace so it syncs).
  - Real-world pattern (documented by users): each agent = a session with
    SOUL.md + AGENTS.md + `/memory/WORKING.md`; multi-agent setups share a
    task DB rather than direct messaging; heartbeats staggered to avoid API
    bursts; cheap models for routine checks.
- **Borrow:** almost the entire personal-agent shape — SOUL.md/USER.md/MEMORY.md
  file layout, gateway + heartbeat + cron trinity, WhatsApp-first messaging.
  This is the closest existing project to open-agent's goal.
- **Avoid:** framework bloat critiques (100k+ lines vs "a directory and a
  crontab") — keep the core small and legible. Also: OpenClaw's model support
  historically centered on a few providers; our LiteLLM layer is the
  differentiator.

## 3. Goose
- URL: https://github.com/aaif-goose/goose (Apache-2.0; Linux Foundation;
  previously at github.com/square/goose)
- What: local, extensible AI agent (desktop/CLI/API) for executing, editing,
  and testing — runs on-device.
- Architecture:
  - **MCP-first extensions:** 70+ MCP extensions; behavior-as-code via YAML
    "Recipes" (a stateless recipe-per-job model).
  - **Providers:** 15+ providers plus local models and ACP.
  - **Governance hooks:** PreToolUse/Stop hooks, read-only mount enforcement,
    headless container runs.
- **Borrow:** MCP as the *primary* tool-integration story (not an afterthought);
  recipes as a declarative way to package repeatable agent jobs — directly
  applicable to our cron-job prompts.
- **Avoid:** the purely stateless recipe model — a personal agent needs
  persistent memory across runs, which Goose de-emphasizes.

## 4. Aider
- URL: https://github.com/Aider-AI/aider (Apache-2.0)
- What: terminal pair-programming agent; edits code via diffs/patches with
  strong git workflows.
- Architecture:
  - **LiteLLM for providers:** the canonical proof that LiteLLM carries a
    real agent across "many providers via LiteLLM."
  - **Repo map:** tree-sitter-based code graph ranked into context — a
    retrieval pattern for large codebases.
  - **Architect mode:** dual-model (strong planner + cheap editor); watch mode
    with `AI!`/`AI?` inline comments.
- **Borrow:** the LiteLLM integration pattern itself; the dual-model
  (planner/worker) cost optimization — maps to our `default` vs `fast` model
  config; repo-map as the future answer to "context too big."
- **Avoid:** Aider is deliberately *not* autonomous-orchestration-first and
  has no native MCP — it solves pair programming, not personal assistance.
  Don't copy its scope; copy its provider layer.

## 5. OpenCode
- URL: https://opencode.ai (MIT-licensed, independent)
- What: terminal-first coding agent platform; ranked #1 ("best overall CLI
  agent platform") in a 2026 community evaluation of CLI agents for provider
  breadth, MCP, skills, plugins, web, and session coverage.
- Architecture: provider-agnostic core, MCP support, skills + plugin system,
  session management and auto-compaction, web search/fetch.
- **Borrow:** the provider-config pattern (75+ providers, local models) and
  the plugin/skill packaging model. Its session + compaction design is the
  reference for our CLI.
- **Avoid:** fast-moving API surface — pin to concepts (provider config,
  sessions, skills), not to OpenCode's implementation details.

## 6. AnyCoder
- URL: https://github.com/he-yufeng/anycoder
- What: ~1300-line Python terminal coding agent — "works with any LLM"
  (DeepSeek, Qwen, GPT, Claude, Gemini, Kimi, Ollama) via LiteLLM.
- Architecture: minimal ReAct loop, 7 builtin tools (bash with dangerous-
  command blocking, read/write/edit/glob/grep, test runner), slash commands
  (`/model` mid-conversation switching, `/compact`, `/plan` approval mode,
  `/sessions`), model aliases (`claude`, `gemini`, `glm`, `ollama/...`).
- **Borrow:** this is the closest thing to a *reference implementation* of our
  Phase 1–2: one file-scale loop + LiteLLM + aliases + `/model` switching.
  Study its tool-call plumbing and dangerous-command gating before writing
  ours.
- **Avoid:** single-file scale is a virtue here, but we need the event log,
  memory files, and scheduler it deliberately omits.

## 7. langgraph-harness
- URL: https://github.com/renenavas/langgraph-harness
- What: minimal, well-documented LangGraph agent harness built to be read and
  extended (by humans and AI agents).
- Architecture:
  - **Typed tool hierarchy** with risk/category metadata on top of
    `BaseTool`.
  - **Risk-based permission layer:** per tool, `allow` / `deny` / `ask`.
  - **Isolated sub-agents** via a `Task` tool.
  - **Durable non-blocking scheduling:** `ScheduleWakeup` suspends the graph
    and resumes from a checkpoint; a `WakeupStore` + worker daemon survives
    restarts.
  - **Automatic history summarization** to bound context; Markdown REPL.
  - **Contract: tools never raise** — bad input returns `ERROR:` with fix
    instructions, keeping the loop self-correcting.
- **Borrow:** the permission-layer design and the `ERROR:`-as-instruction
  contract verbatim; the durable-scheduling pattern (SQLite job store) for our
  cron implementation.
- **Avoid:** LangGraph itself as a dependency for v1 — the *patterns* port
  cleanly to a hand-rolled loop without the framework weight.

## 8. Open Interpreter
- URL: https://github.com/OpenInterpreter/open-interpreter
- What: terminal agent that executes code and actions on your machine —
  "let language models run code locally."
- Architecture: code-execution-centric loop; the model writes Python/shell and
  a local interpreter runs it, results feed back into the loop.
- **Borrow:** the insight that a *code-execution tool* subsumes many bespoke
  tools — our `exec` tool with cwd tracking and timeouts is this idea,
  sandboxed by the permission gate.
- **Avoid:** unconstrained local code execution as the *only* tool interface;
  for a personal agent, typed file tools (read/write/edit with diffs) are
  safer and more legible than raw exec for everyday tasks.

## 9. Continue
- URL: https://github.com/continuedev/continue — **archived; acquired by
  Cursor (Jul 2026), repo read-only, v2.1.0 final.**
- What (was): open-source IDE/CLI assistant, multi-model, local-model and
  privacy focused, with `.continue/checks/` AI semantic checks on PRs.
- **Borrow:** the anti-slop/semantic-check idea (agent-enforced quality gates
  on its own output) is worth reviving as a Phase-9 hardening pattern.
- **Avoid:** the project itself — dead. Its cautionary lesson: depending on a
  single corporate steward is a risk; Apache-2.0 + community governance (like
  Goose under the Linux Foundation) is the safer home for open-agent.

## 10. AutoGen (Microsoft)
- URL: not verified in this research pass (well-known Microsoft Research
  project; URL omitted rather than guessed).
- What: multi-agent conversation framework — agents as conversable entities,
  human-in-the-loop, code execution.
- Architecture: conversation-centric orchestration; supervisor/group-chat
  patterns; the "agents talk to each other" model that the LangGraph
  supervisor literature formalized (supervisor / network / hierarchical
  topologies).
- **Borrow:** the orchestration *vocabulary* (supervisor, handoff, group chat)
  for our subagent design docs — but implement the simple version: a `task`
  tool, not a conversation framework.
- **Avoid:** adopting AutoGen as a dependency — it optimizes for
  conversation-centric research demos, not for a lean personal agent loop.
  (Per engineering-handbook survey: "Reach for multi-agent only when you can
  name the constraint it relieves… A better single agent usually wins.")

---

## Cross-cutting lessons for open-agent

1. **The loop is solved; the differentiators are memory, scheduling, and
   provider breadth.** Every project above runs reason→act→observe. None does
   all three of: markdown-file memory, proactive cron/heartbeat, and true
   model-agnosticism. That gap is open-agent's thesis.
2. **LiteLLM is the proven provider layer** (Aider, AnyCoder, openacm,
   neuralcleave) — but keep tool schemas simple and pin versions; provider
   quirks are the #1 source of field bugs.
3. **MCP is the tool ecosystem bet** (Goose, OpenHands skills+MCP). Build the
   client once, get the ecosystem free.
4. **Permissions are a first-class design axis** (langgraph-harness
   allow/ask/deny, Goose hooks, OpenHands security interleaving) — not a
   post-launch patch.
5. **Markdown files beat databases for personal memory** (OpenClaw lineage) —
   transparent, syncable, debuggable. The user reads what the agent knows.
6. **Boring scheduling wins** (SQLite job store, silent-on-no-change delivery
   rules, max one concurrent run per job).
