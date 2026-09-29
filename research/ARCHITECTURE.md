# open-agent — Architecture Research & Design

Date: 2026-09-29. Status: design doc (no code). This document recommends the
target architecture for `open-agent`, a model-agnostic personal AI agent:
Claude / Gemini / GPT / Ollama / GLM (Zhipu) / any LiteLLM-supported provider,
one agent codebase, same tools, memory, scheduler, and local dashboard
regardless of which model the user picks.

---

## 1. Recommended overall architecture

### The core loop (ReAct, event-driven)

Every successful open-source agent converges on the same shape:

```
user input (chat / cron / hook)
  → build context (system prompt + memory + skills + tool schemas)
  → LLM call (via provider abstraction)
  → response contains tool calls?
      yes → permission check → execute tools (local or MCP) → append results → loop
      no  → final message → persist transcript → done
```

OpenHands formalizes this as an **event-sourced loop**: every user message,
action, and observation is a typed event appended to an immutable `EventLog`;
the loop state is a projection over that log. That gives pause/resume, replay,
fork, and remote observation almost for free (see CASE_STUDIES.md — OpenHands).

**Recommendation for open-agent:** start with a simple synchronous ReAct loop,
but record the transcript as an **append-only event log** (JSONL) from day one.
The event log becomes the substrate for: resume/replay, dashboard "activity"
view, subagent delegation, and later pause/resume. Don't build the full
OpenHands lease/fencing machinery — a plain event log plus a `ConversationStore`
is enough for v1.

### Component map

```
┌─────────────────────────────────────────────────────────────┐
│ Surfaces (chat interfaces)                                  │
│  CLI REPL  │  Local dashboard (FastAPI + SPA)  │  WhatsApp/Telegram adapters │
└────────────────────────┬────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Gateway / Daemon                                           │
│   - session manager (one conversation state per chat/user)  │
│   - message router: inbound → agent loop → outbound          │
│   - runs cron jobs + heartbeat by spawning agent runs        │
└────────────────────────┬────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  AgentCore (the loop)                                       │
│   - context builder (persona files, memory, skills)         │
│   - LLMClient (provider abstraction — LiteLLM)              │
│   - tool executor: builtin tools + MCP clients + skills    │
│   - permission gate (per-tool allow/ask/deny)               │
│   - event log writer                                        │
└──────┬──────────────┬───────────────┬────────────────────────┘
       ▼              ▼               ▼
  Providers      Tool system      Memory system
  (LiteLLM)      (registry +      (markdown files
                  MCP client)      + daily logs)
┌─────────────────────────────────────────────────────────────┐
│  Scheduler                                                  │
│   - cron jobs (SQLite job store, survives restarts)         │
│   - heartbeat (periodic proactive agent wake)              │
│   - event hooks (fire on external events)                  │
└─────────────────────────────────────────────────────────────┘
```

**Design principles (borrowed from the survey):**

1. **Agent and isolation are orthogonal** (OpenHands lesson via the DevClaw
   decision doc): the loop reasons; the sandbox/box is a separate layer. Shell
   tools run with a configurable working directory and a permission gate — not
   by handing the framework ownership of containers on day one.
2. **Memory is files, not a database** (OpenClaw/Muse pattern): if you want to
   remember it, write it to a file. Markdown files the user can read, edit, and
   sync are the durable contract; vector search is an optional later add-on.
3. **Tools never raise on bad input** (langgraph-harness lesson): a tool returns
   `ERROR: <how to fix and retry>`. This keeps the loop self-correcting and is
   cheap to enforce in the tool wrapper.
4. **Events over bespoke representations**: one append-only JSONL event stream
   per session; dashboard, replay, and subagent handoffs are all projections of
   it.

---

## 2. Provider abstraction design (the "any model" requirement)

### Verdict: LiteLLM (Python library `litellm`) is the right choice — verified

Evidence:

- LiteLLM (`BerriAI/litellm` on GitHub) exposes one OpenAI-shaped call site —
  `litellm.completion(model=..., messages=..., tools=...)` / `acompletion` —
  across 100+ providers, routing by model-name prefix.
- Multiple independent open-source agent projects already use exactly this
  pattern for model-agnostic agents:
  - **AnyCoder** (github.com/he-yufeng/anycoder): ~1300-line Python terminal
    agent, "100+ models via litellm", aliases for DeepSeek, Qwen, GPT, Claude,
    Gemini, Kimi, GLM, Ollama, custom OpenAI-compatible endpoints.
  - **Aider** (github.com/Aider-AI/aider): connects to many providers via LiteLLM.
  - **neuralcleave** (github.com/theamitchandra/neuralcleave): provider table
    shows Anthropic, Gemini, OpenAI, DeepSeek, Ollama, Mistral, xAI, Cohere,
    Moonshot/Kimi, **Zhipu/GLM**, Qwen, ERNIE, Doubao, OpenRouter — all behind
    one config.
  - **openacm** (github.com/json55hdz/openacm): "OpenACM uses LiteLLM as a
    unified LLM interface, supporting 100+ providers" (Ollama, OpenAI,
    Anthropic, Gemini).
- Required providers map cleanly:
  - Claude → `claude-sonnet-4-6` / `anthropic/claude-...` (`ANTHROPIC_API_KEY`)
  - Gemini → `gemini/gemini-2.5-flash` (`GEMINI_API_KEY` / `GOOGLE_API_KEY`)
  - GPT → `gpt-5.4` / `gpt-4o` (`OPENAI_API_KEY`)
  - Ollama → `ollama/llama3.1` or `ollama_chat/...` (no key, `ollama_base_url`)
  - GLM/Zhipu → `zhipu/glm-4-plus` (`ZHIPUAI_API_KEY`)

### Known quirks to design around (all verified in community research)

1. **Function calling quality varies by provider.** OpenAI/Azure native
   `json_schema` is solid; Gemini 2.0+ native `responseJsonSchema` is solid;
   Anthropic is *synthesized via tool-use* and has open bugs on
   `minimum`/`maximum` constraints (litellm issues #6766, #21016, open as of
   Feb 2026). Ollama depends on the model (`qwen3`, `llama3.1`, `mistral`
   families are decent; small models hallucinate tool calls). → **Design:**
   keep tool schemas simple (flat objects, string/number/boolean enums, avoid
   `minimum`/`maximum`/deep nesting) so they survive the weakest provider.
2. **Ollama + LiteLLM streaming+tools+async had known bugs (2025).** →
   **Design:** pin a tested `litellm` version, add per-provider integration
   smoke tests (one tool call round-trip per provider in CI with recorded
   fixtures, not live keys), and allow the native `ollama` Python client as a
   fallback path for the local-only configuration (the `autometa` project's
   backend comparison reached the same conclusion).
3. **Token counting is 10–30% off for non-OpenAI models** (tiktoken fallback).
   → Don't enforce hard context limits from LiteLLM's counter; use it for cost
   estimates only, and drive compaction from each provider's reported usage when
   available.
4. **Pricing DB lags new model launches; minor versions break things.** →
   Pin `litellm` in `pyproject.toml`, upgrade deliberately, and treat cost
   reporting as advisory.
5. **Gemini-specific:** thinking-config and context-caching are provider-native
   features LiteLLM doesn't fully surface (Google's `race-condition` project
   dispatches Gemini outside LiteLLM for exactly this reason). → **Design:**
   use LiteLLM for the common path; allow a provider escape hatch
   (`extra_provider_params`) but don't depend on it in core flows.

### Alternatives considered (with evidence)

| Option | Verdict |
|---|---|
| **LiteLLM** | ✅ Chosen. 100+ providers, proven in Aider/AnyCoder/openacm. |
| OpenRouter | Aggregation API — simpler, but adds a middleman (cost, latency, lock-in); doesn't cover Ollama local. |
| Provider-native SDKs + hand-rolled adapter | Maximum control, but re-implements routing/fallback/retry LiteLLM already has. Revisit only if LiteLLM blocks a provider. |
| Pydantic AI / LangChain model layer | Pydantic AI is credible but younger and thinner on providers; LangChain is heavier than needed for a personal agent. |
| Raw OpenAI-compatible endpoint assumption | Works for Ollama/vLLM/local servers, fails for Anthropic's native API and Gemini specifics. |

### Provider config shape (recommended)

```yaml
# ~/.open-agent/config.yaml
models:
  default: claude-sonnet-4-6        # alias → resolved below
  fast: gemini-2.5-flash            # cheap model for heartbeat/summaries
  aliases:
    claude: anthropic/claude-sonnet-4-6
    claude-opus: anthropic/claude-opus-4-6
    gemini: gemini/gemini-2.5-flash
    gpt: gpt-5.4
    glm: zhipu/glm-4-plus
    local: ollama/qwen3:32b
providers:
  anthropic: { api_key_env: ANTHROPIC_API_KEY }
  gemini:    { api_key_env: GEMINI_API_KEY }
  openai:    { api_key_env: OPENAI_API_KEY }
  zhipu:     { api_key_env: ZHIPUAI_API_KEY }
  ollama:    { base_url: "http://localhost:11434" }
fallbacks:
  - [claude-sonnet-4-6, gemini-2.5-flash]   # LiteLLM native fallback chains
```

`open-agent --model glm "…"` resolves the alias, sets env from the OS keyring /
`.env`, and calls `litellm.acompletion`. Model switching mid-conversation is a
supported command (`/model`), same as AnyCoder.

---

## 3. Tool system design (including MCP)

### Tool registry

One registry, three tool sources, uniform schema:

```python
class ToolResult:  # never raises; errors are strings starting with "ERROR:"
    output: str
    ...

class Tool:
    name: str
    description: str
    parameters: dict        # JSON Schema, kept simple (see §2 quirks)
    risk: Literal["read", "write", "exec", "network", "destructive"]
    handler: Callable
```

Built-in tools (day one, mirroring the proven set from AnyCoder/Aider):

- `read_file` (offset/limit), `write_file`, `edit_file` (search/replace with
  uniqueness check + diff), `glob`, `grep`
- `exec` (shell with timeout, cwd tracking, dangerous-command blocklist)
- `web_search`, `web_fetch` (page text; never drive a live browser from the
  agent process — browser work is a separate delegated surface)
- `todo_write` (plan tracking inside a run)

### MCP client support

MCP is transport; the registry is the abstraction. Design:

- `mcp_servers:` section in config: each entry names a command (`npx …`,
  `uvx …`, or a URL for SSE/streamable HTTP), plus an allowlist of tools to
  expose.
- At startup (and on `/mcp reconnect`), the agent spawns each server over
  stdio, calls `tools/list`, and registers each as a `Tool` with
  `risk="network"` by default. Tool calls become `tools/call` JSON-RPC.
- **Trust tiers** (borrowed from the okfsmith governed-write pattern): MCP
  *reads* are allowed broadly; MCP *writes* require the server to be marked
  `trusted: true` in config, otherwise the permission gate asks the user.
- Skill-style playbooks (see §7) can declare `mcp: [server-name]` so a skill
  brings its tools with it.

What to avoid: Goose's marketplace scale on day one. Start with "MCP client
works, tools appear in the registry, writes are gated" — the extension
marketplace is a phase-2 concern.

### Permission gate

Per-tool risk → policy: `allow` (read tools), `ask` (write/exec/network to new
hosts), `deny` (destructive without explicit approval mode). The gate is a
function `check(tool, args, context) -> allow|ask|deny`, consulted before
every execution, with the decision recorded in the event log (OpenHands'
"security interleaving" idea, simplified). CLI asks inline; dashboard/chat
surfaces render an approval card.

---

## 4. Memory system design

Borrow the proven OpenClaw/Muse file layout — it is the most copied personal-agent
memory design in the open-source world for a reason (multiple independent
projects — e.g. github.com/zvinn/zain-agent — converge on the same files):

```
~/.open-agent/
  SOUL.md            # persona, tone, hard limits — injected every run
  IDENTITY.md        # who the agent is
  USER.md            # who the user is, preferences
  AGENTS.md          # operating manual: workspace conventions, lessons
  TOOLS.md           # environment-specific tool quirks
  MEMORY.md          # curated long-term memory (tight, hand-maintained)
  memory/
    2026-09-29.md    # daily log, appended by agent + cron jobs
    people/INDEX.md  # relationship pages
    groups/INDEX.md
  sessions/
    <id>/events.jsonl   # the append-only event log (source of truth)
    <id>/summary.md     # compaction summary when context is trimmed
```

Rules:

- **Write-to-remember:** the agent appends durable facts to `MEMORY.md`/daily
  logs itself; nothing is "remembered" that isn't written to a file.
- **Injection:** each run builds the system prompt from SOUL.md + IDENTITY.md +
  USER.md + MEMORY.md (+ relevant daily-log tail). Skills inject on keyword
  match or explicit request.
- **Compaction:** when the event log nears the model's context budget, a
  summarizer (the `fast`/cheap model) writes `summary.md`; the next run loads
  summary + recent tail. This mirrors Muse's own compaction behavior.
- **No vector DB in v1.** Grep over markdown is the retrieval layer; add
  embeddings later only if recall demonstrably fails.

Uncertainty: daily-log vs. vector memory is a genuine tradeoff — the file
approach wins on transparency/debuggability and user trust (the user can read
everything the agent "knows"); it loses on fuzzy recall at large scale. For a
personal agent, transparency wins.

---

## 5. Subagent orchestration design

Two patterns exist; use both, at different scopes:

1. **In-process `task` tool (primary):** the agent's tool registry includes a
   `task(description, context)` tool that spawns a child agent run with its own
   event log, a narrowed tool set, and a summarized brief. The parent continues
   (or waits); the child's final summary returns as a tool result. This is the
   supervisor/worker pattern (LangGraph supervisor literature; Muse's own
   subagent behavior). Cheap, no infra, parallelizable with `asyncio`.
2. **Session-per-role (later):** named persistent sessions with distinct
   SOUL.md files communicating through a shared task store (the
   startupbros/mission-control-on-Clawdbot pattern: 10 named agents, staggered
   heartbeats, Convex as the shared bus). This is powerful but operationally
   heavy — defer to phase 3+.

Rules for v1: children get a **fresh, minimal context** (brief + relevant
files), never the parent's full transcript; the parent's synthesis is the
deliverable (a child-completion handoff that could be terminal must restate
the full result). Cap fan-out (e.g. max 8 parallel children) and give each
child a token/time budget.

---

## 6. Scheduler design (cron + heartbeat + hooks)

The agent is proactive, not just reactive. Three mechanisms:

| Mechanism | Trigger | What it does |
|---|---|---|
| **cron** | wall-clock schedule (interval / daily time) | spawns an agent run with a fixed prompt; run has full tool access; results delivered per the job's delivery rules (notify on trade/merge/alert, silent otherwise) |
| **heartbeat** | every N minutes, always on | lightweight run: check inboxes, pending approvals, watched items; cheap/fast model; stays silent unless something needs attention |
| **hooks** | external event arrives (webhook, file watch) | spawns a run scoped to the event payload |

Implementation:

- **Job store:** SQLite (`~/.open-agent/scheduler.db`) — jobs, run history,
  last-run timestamps, dedupe keys. Survives restarts (the langgraph-harness
  `WakeupStore` lesson: durable scheduling beats in-memory timers).
- **Runner:** the gateway daemon owns a scheduler thread; each firing spawns an
  agent run in a worker (bounded concurrency, `maxConcurrentRuns: 1` default
  per OpenClaw's proven default — avoid overlapping runs of the same job).
- **Delivery rules per job:** `notify_on: [trade, error]` / `silent_on_no_change`
  — the paper-trading loop and PR-merge watch are the reference
  implementations: notify only on state change or failure, never "all clear"
  spam.
- **Keep it boring:** OS cron is the fallback if the daemon isn't running; the
  daemon path is preferred because it shares sessions, memory, and the
  permission policy.

Uncertainty: sub-minute scheduling and exactly-once delivery are out of scope;
at-least-once with idempotent job prompts is the contract.

---

## 7. Skills (playbooks)

A skill is a directory with `SKILL.md` (frontmatter: name, description, when
to use) plus supporting scripts. The agent:

1. indexes all skills at startup;
2. injects a skill's summary into context when the user's request matches its
   `when to use` (keyword + embedding-free heuristic is fine for v1);
3. follows `SKILL.md` as the procedure, using its scripts and any declared MCP
   servers.

This is the OpenHands `AgentContext`/skills mechanism and the OpenClaw plugins
model, minus the marketplace. Ship ~10 built-in skills (gmail, calendar,
github, shopping, tts/podcast, travel, web research…) — each a thin wrapper
over CLI/API calls the agent can already make.

---

## 8. Dashboard design (local FastAPI + offline SPA)

Follow the **okfsmith dashboard pattern**, which is proven in this workspace:

- **Backend:** FastAPI, bound to `127.0.0.1` only, per-launch token
  (`?token=` or header), all state in local files/SQLite. Endpoints:
  - `GET /api/sessions` — list sessions with status
  - `GET /api/sessions/{id}/events` — event log (the single source of truth)
  - `POST /api/chat` — send a message to a session (SSE stream back)
  - `GET /api/jobs`, `POST /api/jobs` — cron job CRUD + run history
  - `GET /api/memory`, `PUT /api/memory/*` — browse/edit MEMORY.md, daily logs
  - `GET /api/models` — configured providers, live reachability probe
  - `GET /api/tools` — tool registry incl. MCP servers and their status
- **Frontend:** single-page app, **fully offline** (no CDN — vendor the JS/CSS),
  served as static files by the same FastAPI process. Pages: Chat, Sessions,
  Jobs/Scheduler, Memory, Models, Tools/MCP, Activity feed.
- **Realtime:** Server-Sent Events from the event log — the dashboard is a
  *projection* of the event stream, same principle as OpenHands' frontend.
- **Security posture:** localhost-only + token is the whole story for v1;
  document that binding to `0.0.0.0` is unsupported without the user adding
  their own auth/TLS.

What to borrow from okfsmith specifically: the token-per-launch flow, the
offline-asset discipline (CI check that no external URLs are referenced), and
the "dashboard is a read/write view over files the CLI also uses" principle —
never a separate state store.

---

## 9. Chat surfaces (CLI now, messaging later)

- **v1: rich CLI REPL** (prompt_toolkit): streaming output, `/model`,
  `/tools`, `/memory`, `/compact`, `/jobs`, multiline input, slash commands —
  the AnyCoder/OpenCode command set is the reference.
- **v2: WhatsApp/Telegram adapters** in the gateway: inbound message →
  session lookup → agent run → outbound reply. WhatsApp needs either the
  Business API or a bridge (Baileys-style); Telegram Bot API is the easy path.
  OpenClaw's gateway proves the model; the adapters are thin.

---

## 10. Concrete recommended build order (phases)

**Phase 0 — Skeleton (done in scaffold):** repo layout, `pyproject.toml`,
`open-agent` entry point, config file shape, README.

**Phase 1 — Core loop + one provider (the walking skeleton):**
`LLMClient` on LiteLLM with Anthropic first; ReAct loop; builtin tools
(read/write/edit/glob/grep/exec); event-log JSONL; CLI REPL with `/model`
stub. Acceptance: `--model anthropic/claude-sonnet-4-6 "write a file"` works
end to end.

**Phase 2 — Provider breadth:** config-driven aliases + fallbacks; integration
smoke tests per provider (recorded fixtures); `/model` switching;
documented quirks table (§2). Acceptance: same prompt works on Claude, Gemini,
GPT, Ollama, GLM with only `--model` changing.

**Phase 3 — Memory:** file layout (§4), context builder, daily-log appends,
`/memory` commands, compaction via the `fast` model. Acceptance: facts persist
across sessions; user can read everything in `~/.open-agent/`.

**Phase 4 — Tools hardening + MCP client:** permission gate (allow/ask/deny),
MCP stdio client with trust tiers, `ERROR:`-contract on all tools. Acceptance:
an MCP server's tools appear in `/tools` and execute through the loop.

**Phase 5 — Subagents:** `task` tool, parallel fan-out with budgets, result
synthesis. Acceptance: research task fans out to 3 children and the parent
returns one synthesized answer.

**Phase 6 — Scheduler:** SQLite job store, cron + heartbeat in the gateway
daemon, per-job delivery rules, `/jobs` CLI. Acceptance: a 5-minute heartbeat
and a daily job run unattended with silent-on-no-change behavior.

**Phase 7 — Dashboard:** FastAPI + offline SPA per §8, SSE over the event log.
Acceptance: chat, sessions, jobs, memory, models pages all work with no
network beyond localhost.

**Phase 8 — Messaging + skills:** WhatsApp/Telegram adapters; 10 built-in
skills; skill auto-injection. Acceptance: user chats from Telegram; Gmail
skill handles a real email task.

**Phase 9 — Hardening & release:** adversarial tool-permission review,
red-team the prompt-injection surface (memory files and tool outputs are
untrusted data — never instructions), packaging, docs site, PyPI.

### Explicit non-goals for v1

- Vector/RAG memory, multi-user, cloud hosting, extension marketplace,
  OpenHands-grade sandboxing (Docker runtime), voice I/O.

---

## 11. Honest uncertainties

1. **LiteLLM long-term stability.** Community research (Apr 2026) notes
   recurring schema-coercion regressions and pricing-DB lag. Mitigation: pin
   versions, per-provider smoke tests, keep the `LLMClient` interface narrow
   so LiteLLM could be swapped.
2. **Weak-model tool calling.** Small Ollama models hallucinate tool calls;
   GLM function-calling quality via LiteLLM is less battle-tested than
   Claude/GPT in the surveyed projects. Mitigation: simple schemas, a
   "strict mode" that re-asks for JSON on parse failure, and documented
   model recommendations per task tier.
3. **Exact Muse parity is impossible** (proprietary model, infra, product
   code) — the goal is functional parity of the *pattern*, stated openly.
