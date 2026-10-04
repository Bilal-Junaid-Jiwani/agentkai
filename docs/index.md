# agentkai documentation

AgentKai is an open-source personal AI agent that runs on your own computer.
It is model-agnostic: bring any model — Claude, Gemini, GPT, a local Ollama
model, GLM — and the agent keeps the same tools, memory, scheduler, and
dashboard. Everything runs locally first; your data stays on your machine.

Start here:

- [quickstart.md](quickstart.md) — install, first run, dashboard, gateway
- [models.md](models.md) — providers, aliases, fallbacks, BYOK key setup
- [tools.md](tools.md) — built-in tools, the permission gate, MCP servers
- [browser.md](browser.md) — the real Chromium browser engine
- [memory.md](memory.md) — markdown memory: SOUL, USER, daily logs, people
- [scheduler.md](scheduler.md) — cron jobs, heartbeat, nightly dreaming
- [channels.md](channels.md) — messaging gateway: Telegram, Discord, WebChat, WhatsApp
- [skills.md](skills.md) — skill framework + the bundled skill catalog
- [voice.md](voice.md) — speech-to-text and text-to-speech
- [media.md](media.md) — image generation (and the honest video story)
- [devices.md](devices.md) — pair your phone/laptop via the companion app
- [goals.md](goals.md) — durable goals and tracked items
- [dashboard.md](dashboard.md) — the local web dashboard
- [privacy.md](privacy.md) — where your data lives, how keys are handled
- [troubleshooting.md](troubleshooting.md) — common problems and fixes

Honest scope note: this is early-stage software (v0.3.0). Core flows —
agent runs, tools, memory, scheduler, dashboard, channels — are implemented
and tested. Areas marked **experimental** in the docs below are real code
with real limits; they are labeled, not hidden. The
[troubleshooting](troubleshooting.md) page lists known gaps.
