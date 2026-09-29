# Scheduler

Background jobs that run while you're away: cron-style recurring tasks,
one-shot timers, a heartbeat check-in, and nightly "dreaming" (memory
consolidation). Jobs live in SQLite (`~/.agentkai/scheduler.db`), so
they survive restarts.

## Job types

| Type | Schedule | What it runs |
|---|---|---|
| `cron` | `"m h dom mon dow"` (Vixie cron) | an agent prompt or a shell command |
| `at` | `@at:2026-10-01T09:00` | once, at the given time |
| `heartbeat` | cron | a check-in prompt (uses `HEARTBEAT.md` if you wrote one) |
| `dreaming` | cron (usually nightly) | a memory-consolidation prompt over the day's logs |

Two kinds of work: **agent jobs** (`--prompt`) run the agent with the
given prompt; **shell jobs** (`--command`) run a command directly
(legacy/simple use).

## CLI

```bash
# every weekday at 7:00, agent job
agentkai scheduler add morning-brief \
  --schedule "0 7 * * 1-5" \
  --prompt "Summarize today's calendar and unread important email." \
  --model claude

# one-shot: run once at 9:00 tomorrow
agentkai scheduler add remind-call --at 2026-10-01T09:00 \
  --prompt "Remind me to call the bank about the transfer."

# plain shell command, every 15 minutes
agentkai scheduler add disk-check --schedule "*/15 * * * *" \
  --command "df -h / | tail -1"

agentkai scheduler list
agentkai scheduler run-once morning-brief   # run now, ignore schedule
agentkai scheduler run-due                  # run everything currently due
agentkai scheduler remove remind-call
```

## Safety: unattended jobs can't ask

A scheduled job runs without you watching, so it can **never prompt**.
The unattended permission policy is fixed:

- low/medium-risk tools → auto-allowed
- **high-risk tools → denied**

Design your job prompts accordingly: a cron job can read, summarize, and
notify, but it cannot `exec` arbitrary commands or send email without you
— those need an attended run.

## Heartbeat & dreaming

- **Heartbeat** wakes the agent on a schedule with a short check-in
  prompt. Write `~/.agentkai/HEARTBEAT.md` to customize what it checks;
  without it, a built-in checklist is used.
- **Dreaming** runs (typically nightly) over the day's event logs and
  daily memory, consolidating what happened into durable memory. It's
  how the agent's long-term memory stays fresh without manual curation.

## How jobs execute

Each run gets its own run id and append-only event log
(`~/.agentkai/runs/<run_id>/events.jsonl`), same as interactive runs —
inspectable from the [dashboard](dashboard.md) afterwards. Run history is
kept in the scheduler database.

Note: the scheduler runs jobs when you invoke it (`run-due`, typically
from your own cron/systemd timer) — there is no always-on daemon in v1.
Point your system cron at `agentkai scheduler run-due` every minute for
continuous operation.
