# Memory

AgentKai's memory is **plain markdown files** in `~/.agentkai/memory/`.
No database, no embeddings service, no black box: you can read every
file, edit them by hand, and version them with git. The agent reads the
core files at the start of each run and appends what it learns.

## The files

| File | Purpose |
|---|---|
| `SOUL.md` | The agent's persona — how it talks and behaves |
| `USER.md` | Who you are: name, timezone, what you care about |
| `MEMORY.md` | Durable facts, preferences, commitments |
| `YYYY-MM-DD.md` | Daily log, one file per day |
| `people/<name>.md` | One page per person in your life |
| `groups/<name>.md` | One page per group/community |

`people/` and `groups/` each keep an `INDEX.md` listing everyone.

## How it works

- **Startup:** the agent loads SOUL, USER, and MEMORY into context, so it
  starts every conversation already knowing you.
- **Learning:** when something durable comes up (a preference, a decision,
  a commitment), it appends to `MEMORY.md` or the right person's page.
- **Daily logs:** `append_daily("...")` adds a timestamped line to today's
  file — a running journal of what happened.
- **Search:** `search("query")` does keyword retrieval across all memory
  files and returns ranked hits with file and line references.

Everything is API-accessible too (`agentkai.memory.Memory`), and the
[dashboard](dashboard.md) has a memory browser page.

## Conventions that keep it useful

- **Facts, not essays.** One line per fact in `MEMORY.md`; detail lives in
  daily logs.
- **People pages** hold what you told the agent about someone and how you
  relate — never inferred sensitive attributes.
- **Edit freely.** The agent re-reads the files each run, so hand-edits
  take effect immediately. If the agent wrote something wrong, fix the
  file — don't argue with it.
- **Back it up.** It's just markdown: `git init` in the memory dir or copy
  it wherever you keep backups.

## Privacy

Memory lives only on your machine ([privacy.md](privacy.md)). It is sent
to your model provider as context with your prompts — that's how the
model "knows" you. With a local model (`--model local`), it never leaves
the box.
