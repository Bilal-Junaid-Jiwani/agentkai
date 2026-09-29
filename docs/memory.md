# Memory

Plain markdown files in `~/.agentkai/memory/`. Human-editable, git-friendly.

- `SOUL.md` — the agent's persona
- `USER.md` — who the user is, preferences
- `MEMORY.md` — durable facts and commitments
- `YYYY-MM-DD.md` — daily logs

The agent reads these at startup (assembled into the system prompt context)
and appends what it learns. No database, no vector store required for v1.
