# Goals & tracking

Two related systems for things that outlive one conversation, both in
SQLite (`~/.agentkai/goals.db`):

- **Goals** — long-lived outcomes you're working toward ("ship the v1
  launch", "learn conversational Urdu"), with subgoals and an activity
  log of progress and setbacks.
- **Tracked items** — concrete commitments with an outcome: reservations,
  deliveries, reminders. Open when the commitment becomes concrete, close
  with evidence when it's resolved.

The agent has tools for both, so it can manage them on your behalf; you
can also drive them from the CLI.

## CLI

```bash
agentkai goals create "Launch v1" --desc "Public release of the agent"
agentkai goals create "Write announcement post" --parent 1   # subgoal
agentkai goals list
agentkai goals show 1
agentkai goals log 1 --kind progress --text "Dashboard QA passed"
agentkai goals close 1 --outcome completed --note "Shipped 2026-10-01"

agentkai track open "Dentist appointment Thursday" --kind reminder
agentkai track list
agentkai track close 1 --evidence "Confirmed by SMS"
```

Goal statuses: `active`, `paused`, `completed`, `abandoned`. Activity
kinds: `progress`, `setback`, `note`. Tracked-item kinds: `reservation`,
`delivery`, `reminder`, `commitment`, `other`.

## The daily briefing

```bash
agentkai goals briefing
```

Prints today's goal-briefing prompt — designed to be wired as a
[scheduler](scheduler.md) job (e.g. every morning at 7:00), so the agent
reviews your goals, checks what's stale, and nudges you on what needs
attention. The prompt is assembled from your active goals and recent
activity.

## How the agent uses it

When you mention an enduring aspiration or a concrete commitment, the
agent opens a goal or tracked item itself. It logs progress as work
happens, and closes tracked items when the outcome is resolved (a
delivery received, a reservation confirmed). Closing needs evidence —
the agent records what proved the outcome, it doesn't just mark done.

## What's not here

- No deadlines/reminders engine beyond what the scheduler provides —
  pair a goal review with a scheduler heartbeat job for nudges.
- No collaboration/sharing: goals are yours, on your machine.
