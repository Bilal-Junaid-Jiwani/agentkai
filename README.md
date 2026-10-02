<p align="center">
  <img src="https://raw.githubusercontent.com/Bilal-Junaid-Jiwani/agentkai/main/assets/agentkai-logo.gif" width="160" height="160" alt="agentkai logo — animated fluffy yeti mascot">
</p>

# agentkai

[![PyPI](https://img.shields.io/pypi/v/agentkai.svg)](https://pypi.org/project/agentkai/)
[![Python](https://img.shields.io/pypi/pyversions/agentkai.svg)](https://pypi.org/project/agentkai/)
[![License](https://img.shields.io/github/license/Bilal-Junaid-Jiwani/agentkai.svg)](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-website-F59E0B)](https://bilal-junaid-jiwani.github.io/agentkai/)
[![Website](https://img.shields.io/badge/website-live-0ea5e9)](https://bilal-junaid-jiwani.github.io/agentkai/site/)

**An open-source personal AI agent that runs on your own computer.**

agentkai is model-agnostic by design: bring your own model — Claude,
Gemini, GPT, a local Ollama model, GLM, or anything LiteLLM speaks —
and the agent keeps the same tools, memory, scheduler, and dashboard.
Everything runs locally first; your data stays on your machine.

![agentkai dashboard — chat with the agent](https://raw.githubusercontent.com/Bilal-Junaid-Jiwani/agentkai/main/docs/assets/img/dashboard-chat.png)

## 60-second quickstart

```bash
pip install agentkai

export ANTHROPIC_API_KEY=...   # or GEMINI_API_KEY / OPENAI_API_KEY / ZHIPUAI_API_KEY
agentkai run "summarize what's in ~/Documents" --model claude
agentkai dashboard             # local UI at http://127.0.0.1:8931/
```

No key? Install [Ollama](https://ollama.com), `ollama pull qwen3:32b`,
then `agentkai run "hello" --model local` — fully offline.

> ⭐ If agentkai helped you, a star means a lot — it helps other developers find the project.

## Why agentkai

Cloud assistants rent you a brain. agentkai gives you the whole animal:
one agent loop with real tools, a memory that persists in plain markdown,
a scheduler that works while you sleep, and a local dashboard — all wired
to whichever model you choose, on your own machine.

- **Model-agnostic, BYOK.** Aliases (`claude`, `gemini`, `gpt`, `glm`,
  `local`) with fallbacks and capability probing. Keys are yours, never
  logged, never leave your machine except to the provider you chose.
- **Real tools, permission-gated.** Shell/filesystem (root-scoped,
  blocklisted), a real Chromium browser, an MCP client (stdio + SSE),
  and 12 bundled skills: Gmail, Calendar, GitHub, Spotify, places,
  images, voice, media, payments, shopping, travel, health. Every tool
  call passes a risk-based allow/ask/deny gate, audited — unattended
  jobs can never run high-risk tools.
- **Markdown memory.** `SOUL.md` / `USER.md` / `MEMORY.md`, daily logs,
  people & groups, keyword search. Plain files you can read, edit, and
  git — no black-box vector store.
- **Scheduler.** Cron, one-shot, heartbeat check-ins, and nightly
  dreaming (memory consolidation), all in SQLite. Manage jobs from the
  CLI or the dashboard — create, delete, run now.
- **Messaging gateway.** Telegram, Discord, built-in WebChat, and an
  experimental WhatsApp bridge. Per-session isolation; owner approvals
  via `APPROVE` / `DENY`.
- **Voice & media.** Speech-to-text/TTS via faster-whisper or OpenAI;
  image generation via OpenAI or a local Stable Diffusion server.
  Honest errors when nothing is configured — nothing faked.
- **Devices & goals.** Pair your phone via the companion-app contract;
  durable goals and tracked items with a daily briefing prompt.

## CLI reference

| Command | What it does |
|---|---|
| `agentkai run PROMPT` | run one agent task with live streaming output |
| `agentkai chat PROMPT` | send one prompt, print the final answer |
| `agentkai models` | show model aliases and what they resolve to |
| `agentkai dashboard` | launch the local web dashboard (127.0.0.1 only, token-secured) |
| `agentkai gateway` | run the messaging gateway daemon (Telegram/Discord/WebChat/WhatsApp) |
| `agentkai scheduler add/list/remove/run-once/run-due` | manage scheduled agent jobs |
| `agentkai devices` | pair phones/laptops and reach them from the agent |
| `agentkai goals` | durable user goals: create, track, close |
| `agentkai track` | tracked items: reservations, deliveries, reminders |
| `agentkai skills list/show/install` | manage SKILL.md playbooks |

Run `agentkai <command> --help` for flags on any command.

## Web dashboard

Prefer a UI? `agentkai dashboard` launches a local web app: chat with
the agent, watch runs stream in with tool calls inline, browse memory,
and manage scheduled jobs — create, delete, or trigger a run now.

```bash
agentkai dashboard
# → http://127.0.0.1:8931/?token=<fresh-token-every-launch>
```

It binds only to `127.0.0.1` (never exposed to the network), generates
a fresh login token on every launch, and loads zero third-party
resources — the entire UI is packaged with agentkai and works fully
offline. Same agent, same tools underneath.

![agentkai dashboard — chat](https://raw.githubusercontent.com/Bilal-Junaid-Jiwani/agentkai/main/docs/assets/img/dashboard-chat.png)
![agentkai dashboard — scheduler job management](https://raw.githubusercontent.com/Bilal-Junaid-Jiwani/agentkai/main/docs/assets/img/dashboard-scheduler.png)

## Docs

📚 **Documentation:** [`docs/`](https://github.com/Bilal-Junaid-Jiwani/agentkai/tree/main/docs)

[Quickstart](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/quickstart.md) ·
[Models](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/models.md) ·
[Tools](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/tools.md) ·
[Browser](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/browser.md) ·
[Memory](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/memory.md) ·
[Scheduler](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/scheduler.md) ·
[Channels](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/channels.md) ·
[Skills](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/skills.md) ·
[Voice](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/voice.md) ·
[Media](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/media.md) ·
[Devices](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/devices.md) ·
[Goals](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/goals.md) ·
[Dashboard](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/dashboard.md) ·
[Privacy](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/privacy.md) ·
[Troubleshooting](https://github.com/Bilal-Junaid-Jiwani/agentkai/blob/main/docs/troubleshooting.md)

Honest scope note: this is early-stage software (v0.2.0). Core flows —
agent runs, tools, memory, scheduler, dashboard, channels — are
implemented and tested. Areas marked **experimental** in the docs are
real code with real limits; they are labeled, not hidden.

## Development

```bash
git clone https://github.com/Bilal-Junaid-Jiwani/agentkai
cd agentkai
python -m venv .venv && .venv/bin/pip install -e ".[test,voice]"
.venv/bin/python -m pytest tests/   # full suite
```

## License

Apache-2.0. See [LICENSE](LICENSE).
