# Tools

## Built-in tools

- `exec` — run shell commands on the agent's computer
- `read_file` / `write_file` — filesystem access

## MCP servers

Any MCP server (stdio) can be attached as remote tools — files, databases,
browser automation, third-party APIs. Write tools are trust-tier gated
(preview before apply), same pattern as governed write-back systems.

## Skills

Reusable playbooks as markdown files (e.g. Gmail triage, shopping research).
The agent reads the skill file and follows it. Skills live in
`~/.agentkai/skills/`.

## Permissions

Risk-based gate per tool call: `allow` (read-only), `ask` (writes, sends),
`deny` (destructive without approval). Configured in `~/.agentkai/config.yaml`
(planned).
