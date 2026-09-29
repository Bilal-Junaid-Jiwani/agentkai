# Privacy

## Local-first

AgentKai runs on **your computer**. The agent loop, tools, memory files,
scheduler database, dashboard, and messaging gateway are all local
processes reading and writing `~/.agentkai/`. There is no AgentKai
cloud, no account, no telemetry, no analytics.

## What leaves the machine

Exactly what you choose to send, and nothing else:

- **Your model provider.** Hosted models (Claude, Gemini, GPT, GLM)
  receive your prompts — including memory context — as API requests.
  That's the service working as intended. With `--model local`
  (Ollama), nothing leaves the box.
- **Skill APIs you configured.** Gmail/Calendar/GitHub/Spotify tools
  call those services' APIs with your tokens, because you asked them to.
- **Nothing else.** No crash reports, no usage stats, no update checks.

## How keys are handled

- Keys come from environment variables (recommended) or
  `~/.agentkai/*_token.json` files. Environment always wins.
- Keys are **never printed, never logged.** `agentkai models` shows
  only `set`/`missing`. Error messages carry status codes, not key
  material. Dashboard redaction uses the same rule.
- Device pairing tokens are stored as SHA-256 hashes, not the tokens
  themselves.
- Never commit `~/.agentkai/` to git. Skill setup docs say this per
  skill; it bears repeating: the directory holds your keys.

## Memory and logs

Memory is markdown on your disk — back it up like any personal files.
Event logs (`~/.agentkai/runs/*/events.jsonl`) record full tool inputs
and outputs; they may contain sensitive text you worked with. They're
yours, local, and deletable: remove a run's directory and it's gone.

## Boundaries the agent holds

- Public actions (sending email, posting, publishing, charging money)
  always go through the permission gate — the agent asks first.
- It never invents recipients, never handles raw card numbers, never
  identifies people from images.
- Unattended scheduler jobs can't run high-risk tools at all.
