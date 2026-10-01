<p align="center">
  <img src="assets/agentkai-logo.gif" width="160" height="160" alt="agentkai logo — animated fluffy mascot">
</p>

# agentkai

An open-source personal AI agent that runs on your own computer.
Model-agnostic by design: bring your own model — Claude, Gemini, GPT,
Ollama (local), GLM, or anything LiteLLM speaks — and the agent keeps
the same tools, memory, scheduler, and dashboard.

```
                    ┌─────────────┐
                    │    You      │
                    └──────┬──────┘
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  ┌───────────┐     ┌─────────────┐     ┌─────────────┐
  │   CLI     │     │  Dashboard  │     │   Gateway   │
  │agentkai   │     │ 127.0.0.1   │     │ Telegram /  │
  │run · chat │     │ token-auth  │     │ Discord /   │
  └─────┬─────┘     └──────┬──────┘     │ WebChat /   │
        │                  │            │ WhatsApp*   │
        └──────────────────┼────────────┴──────┬──────┘
                           ▼                   ▼
                    ┌──────────────────────────────┐
                    │        Agent loop            │
                    │  think → tool-call → observe │
                    │  streaming · timeouts ·      │
                    │  approval gates · event log  │
                    └──────────────┬───────────────┘
        ┌────────────┬─────────────┼──────────────┬────────────┐
        ▼            ▼             ▼              ▼            ▼
  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────┐
  │ Providers│ │  Tools   │ │  Browser   │ │  Memory  │ │Scheduler │
  │ Claude   │ │ exec etc │ │ Chromium   │ │ markdown │ │ cron ·   │
  │ Gemini   │ │ MCP      │ │ real pages │ │ SOUL/USER│ │ heartbeat│
  │ GPT/Oll. │ │ 12 skills│ │ clicks/forms│ │ daily log│ │ dreaming │
  └──────────┘ └──────────┘ └────────────┘ └──────────┘ └──────────┘
```

\* WhatsApp via an experimental user-run bridge sidecar.

## Features

- **Model-agnostic** — aliases (`claude`, `gemini`, `gpt`, `glm`,
  `local`) with fallbacks, capability probing, BYOK keys that are never
  logged. Fully offline with Ollama.
- **Real tools** — shell/filesystem (root-scoped, blocklisted), a real
  Chromium browser, MCP client (stdio + SSE), 12 bundled skills (Gmail,
  Calendar, GitHub, Spotify, places, images, voice, media, payments,
  shopping, travel, health).
- **Permission gates** — risk-based allow/ask/deny on every tool call,
  audited. Unattended jobs can never run high-risk tools.
- **Markdown memory** — SOUL/USER/MEMORY.md, daily logs, people & groups,
  keyword search. Plain files you can edit and git.
- **Scheduler** — cron/at jobs, heartbeat check-ins, nightly dreaming
  (memory consolidation), all in SQLite.
- **Messaging gateway** — Telegram, Discord, built-in WebChat, and an
  experimental WhatsApp bridge; per-session isolation, owner approvals
  via `APPROVE`/`DENY`.
- **Local dashboard** — FastAPI on 127.0.0.1 only, per-launch token,
  offline SPA, live SSE streaming, run replay.
- **Voice & media** — faster-whisper / OpenAI STT+TTS; image generation
  via OpenAI (paid) or local Stable Diffusion. Honest errors when
  nothing is configured — nothing faked.
- **Devices & goals** — pair your phone via the companion-app contract;
  durable goals and tracked items with a daily briefing prompt.

## Quick start

```bash
pip install agentkai
export ANTHROPIC_API_KEY=...   # or GEMINI_API_KEY / OPENAI_API_KEY / ZHIPUAI_API_KEY
agentkai run "summarize what's in ~/Documents" --model claude
agentkai dashboard             # local UI at http://127.0.0.1:8931/
```

No key? Install [Ollama](https://ollama.com), `ollama pull qwen3:32b`,
then `agentkai run "hello" --model local` — fully offline.

Full guide: [`docs/quickstart.md`](docs/quickstart.md) ·
all docs: [`docs/index.md`](docs/index.md)

## Layout

- `src/agentkai/` — the package: agent loop, providers, tools, browser,
  channels, dashboard, devices, memory, scheduler, skills, voice, media,
  goals, widgets
- `docs/` — user documentation for every feature (original docs)
- `research/` — architecture research and case studies
- `examples/` — example configs and skills

## Status

v0.2.0 — core flows (agent runs, tools, memory, scheduler, dashboard,
channels) are implemented and tested; the dashboard now manages
scheduled jobs (create, delete, run now). Items marked **experimental** in
the docs (WhatsApp bridge, video generation) are real code with real
limits. See [`docs/troubleshooting.md`](docs/troubleshooting.md) for
known gaps.

## Development

```bash
git clone https://github.com/Bilal-Junaid-Jiwani/agentkai
cd agentkai
python -m venv .venv && .venv/bin/pip install -e ".[test,voice]"
.venv/bin/python -m pytest tests/   # full suite
```

## License

Apache-2.0
