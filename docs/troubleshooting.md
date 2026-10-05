# Troubleshooting

Start here:

```bash
agentkai doctor
```

It checks the config file, provider keys, scheduler database, memory
tree, tool registry, image media providers, and the bundled dashboard
assets, then prints one `[OK]`/`[WARN]`/`[FAIL]` line per check (exit
code 1 when anything fails). `--json` gives a machine-readable report.

## Install / CLI

**`agentkai: command not found`** — the pip install didn't put scripts
on your PATH. Try `python -m pip install agentkai` (same Python you'll
run it with) and check `~/.local/bin` is on PATH.

**Import errors after install** — you may have installed into a
different Python than the one running `agentkai`. Use one venv and
install inside it.

## Models

**`run error: ...` / auth failures** — the model alias resolved, but the
provider rejected the call. Check `agentkai models`: the key shows
`missing`, or the key is wrong/revoked. Keys are never printed, so
re-export the env var carefully.

**`--model local` fails immediately** — Ollama isn't running or the
model isn't pulled: `ollama pull qwen3:32b` (or change the `local`
alias in `~/.agentkai/config.yaml`). This fails fast and cleanly by
design — no hanging.

**Model ignores tool calls** — small local models vary in function-
calling ability. Try a larger Ollama model or a hosted alias. The
capability probe (`probe_capabilities`) can confirm what a model
supports.

**Fallbacks firing constantly** — usually a bad key or a provider
outage. Fix the primary; fallbacks are a safety net, not a setup.

## Dashboard

**Forgetting the token** — it's printed once at launch. Restart
`agentkai dashboard` for a fresh token.

**401 on every request** — pass `?token=…` in the URL or the
`Authorization: Bearer` header. `EventSource` can't set headers, so the
UI uses the query param.

**Port in use** — `agentkai dashboard --port 9000`.

**Dashboard won't bind a LAN address** — correct. It refuses anything
but 127.0.0.1, deliberately.

## Gateway / channels

**`agentkai gateway` exits / channel won't connect** — read the log
line: it's usually a missing token env var or a malformed
`~/.agentkai/channels.yaml`. The first run writes an annotated example;
diff yours against it.

**Telegram bot doesn't reply** — the bot needs your chat id in the
channel's `allowlist`, and it must have seen at least one message from
you (send it anything first).

**Discord: no message content** — enable the *Message Content*
privileged intent in the developer portal, then restart the gateway.

**WhatsApp sidecar QR won't scan / gets rate-limited** — experimental
bridge; use a spare number, keep the sidecar on the same machine, and
expect WhatsApp to throttle automated web clients.

**Approval timed out** — no reply within `approval_timeout` (default
300s) counts as **deny**. Reply `APPROVE <id>` / `DENY <id>` faster, or
raise the timeout.

## Scheduler

**Jobs never run** — v1 has no always-on daemon: point your system cron
at `agentkai scheduler run-due` every minute. Check `agentkai scheduler
list` for `last_run`.

**Cron job failed silently** — inspect the job's run event log under
`~/.agentkai/runs/`; the summary is also in the scheduler database.

**Job needs a high-risk tool** — unattended jobs deny high-risk tools
by design. Restructure the prompt to avoid them, or run it attended.

## Skills

**Every tool returns a config error** — the skill's credential is
missing. Run `agentkai skills show <name>` and follow its Setup section.
Most need an OAuth/API token in an env var or `~/.agentkai/*_token.json`.

**OAuth token expired** — expected (~1 hour lifetime). Rotate the token
file or env var; skills re-read it on every call. AgentKai doesn't do
the OAuth dance for you.

**Spotify playback does nothing** — playback targets the active device.
Open Spotify on a device and start anything playing first.

## Voice / media / browser

**`transcribe_audio` errors** — install the voice extra
(`pip install agentkai[voice]`) for offline STT, or set
`OPENAI_API_KEY`. Non-wav input needs `ffmpeg`.

**`generate_image` errors** — no provider configured. Set
`OPENAI_API_KEY` (paid) or run local Stable Diffusion with `--api`.

**Browser tools raise `BrowserUnavailable`** — `pip install playwright`
then `playwright install chromium`.

## Still stuck?

Run with the failing command's full output, check
`~/.agentkai/runs/<run_id>/events.jsonl` for the exact tool error, and
open an issue at
https://github.com/Bilal-Junaid-Jiwani/agentkai/issues with the log
(redact any keys first — they should never appear, but check).
