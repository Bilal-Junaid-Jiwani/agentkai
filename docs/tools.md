# Tools

Tools are what the agent can *do*. Every tool is a plain Python callable
described by a JSON Schema, so every model provider that supports function
calling (via LiteLLM) gets the same toolset. Tool calls are normalized
across providers — the tools never know which model is behind them.

## Built-in tools

| Tool | What it does | Risk |
|---|---|---|
| `exec` | Run a shell command (timeout, cwd-scoped, destructive-command blocklist) | high |
| `read_file` | Read a text file (offset/limit paging) | low |
| `write_file` | Write/append a file (overwrite or append) | medium |
| `list_dir` | List directory entries | low |
| `fetch_url` | GET a web page as text (size-capped) | medium |

Plus capability modules that add their own tools:

- **browser** — navigation, clicking, forms, screenshots, downloads ([browser.md](browser.md))
- **MCP** — any configured MCP server's tools, prefixed `mcp_<server>_<tool>`
- **skills** — each loaded skill contributes its tools ([skills.md](skills.md))
- **devices** — `device_notify`, SMS/contacts/calendar via the companion app ([devices.md](devices.md))
- **voice** — `transcribe_audio`, `speak_text` ([voice.md](voice.md))
- **media** — `generate_image`, `edit_image`, experimental `generate_video` ([media.md](media.md))
- **goals** — goal/tracked-item management for the agent ([goals.md](goals.md))
- **widgets** — `render_widget`: option buttons, forms, cards, map pins in chat

## Safety model

File and shell tools are **root-scoped**: paths are resolved (symlinks
included) and must stay inside the allowed roots — `..` escapes and symlink
tricks are rejected. `exec` additionally refuses obviously destructive
commands (`rm -rf /`, disk wipes, fork bombs). This is a blocklist, not a
sandbox: it stops accidents and casual abuse, not a determined attacker.
`fetch_url` refuses non-HTTP(S) URLs, embedded credentials, and non-public
hosts, and re-validates every redirect hop.

## The permission gate

Every tool call passes through a risk-based gate with three decisions:

- **low risk** (reading files, listings) → allowed automatically
- **medium / high risk** → the gate **asks** you first
- anything you configure → allowed or denied by policy

In the terminal, "ask" means an interactive prompt. In the messaging
gateway, the owner gets an `APPROVE <id>` / `DENY <id>` message
([channels.md](channels.md)). Every decision is auditable.

Policy overrides live in code or config:

```python
PermissionGate(policy={
    "risk:high": "deny",      # non-interactive lockdown
    "tool:exec": "ask",       # always ask for shell, even if policy says allow
})
```

Tool-specific entries beat risk-level entries, which beat the built-in
defaults. Scheduled (unattended) jobs run with low/medium auto-allowed and
high **denied** — a cron job can never prompt you, and can never run a
high-risk tool. Pass `agentkai run -y` only when you consciously accept
auto-approval.

## MCP servers

AgentKai is an MCP **client** (no MCP server is bundled). Configure servers
in `~/.agentkai/mcp.json`:

```json
{
  "servers": {
    "my-db": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "my_mcp_server"]
    }
  }
}
```

On startup the agent connects every configured server (stdio or SSE),
lists its tools, and exposes them as `mcp_<server>_<tool>`. A server that
fails to start is skipped with a warning — one bad server can't take down
the agent.

## Writing your own tool

```python
from agentkai.tools import Tool

def reverse(text: str) -> str:
    return text[::-1]

tool = Tool(
    name="reverse",
    description="Reverse a string.",
    json_schema={"type": "object",
                 "properties": {"text": {"type": "string"}},
                 "required": ["text"]},
    risk="low",
    func=reverse,
)
```

Register it on a `Registry` and hand the registry to the `Agent`. For
reusable, documented, installable capabilities, package it as a
[skill](skills.md) instead.
