# Quickstart

Get from zero to a working agent in a few minutes.

## 1. Install

Requires Python 3.10+.

```bash
pip install agentkai
```

Or from source:

```bash
git clone https://github.com/Bilal-Junaid-Jiwani/agentkai
cd agentkai
pip install .
```

Verify:

```bash
agentkai --help
```

## 2. Pick a model (BYOK)

AgentKai has no model of its own — you bring your own key. Set the key for
the provider you want:

| Provider | Key env var | Alias |
|---|---|---|
| Anthropic Claude | `ANTHROPIC_API_KEY` | `claude` |
| Google Gemini | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini` |
| OpenAI GPT | `OPENAI_API_KEY` | `gpt` |
| Zhipu GLM | `ZHIPUAI_API_KEY` | `glm` |
| Ollama (local, free) | none — runs on your PC | `local` |

```bash
export ANTHROPIC_API_KEY=sk-ant-...
agentkai models        # shows aliases + which keys are set
```

For fully local, install [Ollama](https://ollama.com), pull a model
(`ollama pull qwen3:32b`), and use `--model local` — no key, no network,
no cost.

## 3. First run

```bash
agentkai run "list the files in my home directory and summarize what this machine is used for"
```

The answer streams as the model thinks. When a tool needs your approval
(medium/high risk), the CLI asks in the terminal. Pass `-y` to auto-approve
— only do this when you trust the task.

One-shot answer without the loop UI:

```bash
agentkai chat "what day is it?" --model gemini
```

## 4. Open the dashboard

```bash
agentkai dashboard
```

This prints a one-time URL like `http://127.0.0.1:8931/?token=…`.
The dashboard binds **127.0.0.1 only** (never your LAN, never the internet)
and requires the per-launch token. It shows chat, run history with event
timelines, memory files, and scheduler jobs. See [dashboard.md](dashboard.md).

## 5. Talk from your phone (optional)

```bash
agentkai gateway
```

Starts the messaging gateway with WebChat on
`http://127.0.0.1:18790/`. Enable Telegram/Discord in
`~/.agentkai/channels.yaml` to chat from those apps. See
[channels.md](channels.md).

## 6. Make it remember

Memory is plain markdown in `~/.agentkai/memory/`. Edit `USER.md` to tell
the agent who you are; it appends daily logs itself. See [memory.md](memory.md).

## 7. Automate

```bash
agentkai scheduler add morning-brief --schedule "0 7 * * *" \
  --prompt "Summarize today's calendar and any unread important email."
agentkai scheduler list
```

Jobs run even when you're not watching (see [scheduler.md](scheduler.md)).

## What's next

- [models.md](models.md) — fallbacks, custom aliases, capability probing
- [tools.md](tools.md) — what the agent can do, and how approvals work
- [skills.md](skills.md) — Gmail, Calendar, GitHub, Spotify and more
- [privacy.md](privacy.md) — where your data and keys live
