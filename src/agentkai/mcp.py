"""MCP client: attach Model Context Protocol servers as agentkai tools.

Only the *client* side is implemented (no MCP server here). Supported
transports:

- ``stdio``: spawn ``command`` and speak newline-delimited JSON-RPC 2.0 over
  its stdin/stdout (the standard MCP stdio framing).
- ``sse``: the pre-2025-03 MCP HTTP transport — open a GET event stream,
  read the ``endpoint`` event, POST JSON-RPC messages to it, and receive
  responses as ``message`` events on the stream.

Connection config lives in ``~/.agentkai/mcp.json``::

    {"servers": {
        "filesystem": {
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"DEBUG": "1"},
            "timeout": 30,
            "tools_allowlist": ["read_file"]
        },
        "web": {"transport": "sse", "url": "http://localhost:8000/sse"}
    }}

MCP tools are translated to local :class:`agentkai.tools.Tool` objects
(name, description, json_schema, risk, callable) so the agent loop treats
remote and built-in tools identically. Remote tools default to risk
"medium": reads flow freely, writes should be gated by the permission
policy (see research/ARCHITECTURE.md section 3).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .tools import Tool

DEFAULT_CONFIG = Path("~/.agentkai/mcp.json").expanduser()
DEFAULT_TIMEOUT = 30
CLIENT_INFO = {"name": "agentkai", "version": "0.2.0"}
PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """Transport, protocol, or server-side MCP failure."""


# ---- transports -------------------------------------------------------------


class Transport:
    """JSON-RPC 2.0 request/response channel to one MCP server."""

    def request(self, method: str, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _Pending:
    """Thread-safe id -> response slot map."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[int | str, queue.Queue] = {}
        self._next_id = 0

    def new(self) -> tuple[int, queue.Queue]:
        with self._lock:
            self._next_id += 1
            q: queue.Queue = queue.Queue(maxsize=1)
            self._slots[self._next_id] = q
            return self._next_id, q

    def resolve(self, msg_id: int | str, message: dict) -> bool:
        with self._lock:
            q = self._slots.pop(msg_id, None)
        if q is None:
            return False
        q.put(message)
        return True

    def drop(self, msg_id: int | str) -> None:
        with self._lock:
            self._slots.pop(msg_id, None)

    def fail_all(self, exc: Exception) -> None:
        with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        for q in slots:
            q.put(exc)


def _check_rpc(message: dict, method: str) -> Any:
    if isinstance(message, Exception):
        raise MCPError(f"MCP request {method!r} failed: {message}")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise MCPError(f"MCP request {method!r} got a malformed response")
    if "error" in message and message["error"] is not None:
        err = message["error"]
        raise MCPError(
            f"MCP request {method!r} failed: "
            f"{err.get('message', err)} (code {err.get('code')})"
        )
    return message.get("result")


class StdioTransport(Transport):
    """Speak MCP over a child process's stdin/stdout (newline-delimited JSON)."""

    def __init__(self, command: list[str], env: dict | None = None,
                 cwd: str | None = None) -> None:
        merged_env = dict(os.environ)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=merged_env,
                cwd=cwd,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPError(f"Could not start MCP server {command!r}: {exc}")
        self._pending = _Pending()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-stdio-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    self._pending.resolve(msg["id"], msg)
                # Server notifications (no id) are ignored in v1.
        except Exception as exc:  # noqa: BLE001
            self._pending.fail_all(exc)
        finally:
            self._stop.set()

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload) + "\n"
        try:
            with self._write_lock:
                assert self._proc.stdin is not None
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"MCP stdio write failed: {exc}")

    def request(self, method: str, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        if self._proc.poll() is not None:
            raise MCPError(
                f"MCP server exited (code {self._proc.returncode}) before {method!r}"
            )
        msg_id, slot = self._pending.new()
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method,
                        "params": params or {}})
            try:
                message = slot.get(timeout=timeout)
            except queue.Empty:
                raise MCPError(f"MCP request {method!r} timed out after {timeout}s")
            return _check_rpc(message, method)
        finally:
            self._pending.drop(msg_id)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        self._stop.set()
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except OSError:
            pass
        self._reader.join(timeout=5)


class SSETransport(Transport):
    """MCP's SSE transport: GET the event stream, POST messages to the
    endpoint URI announced by the ``endpoint`` event."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._pending = _Pending()
        self._endpoint: str | None = None
        self._endpoint_event = threading.Event()
        self._stop = threading.Event()
        self._post_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-sse-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            req = urllib.request.Request(
                self._url, headers={"Accept": "text/event-stream"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                event: str | None = None
                data_lines: list[str] = []
                while not self._stop.is_set():
                    line = resp.readline().decode("utf-8", errors="replace")
                    if line == "":
                        break  # stream closed
                    line = line.rstrip("\n").rstrip("\r")
                    if line == "":
                        self._dispatch(event, "\n".join(data_lines))
                        event, data_lines = None, []
                    elif line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
        except Exception as exc:  # noqa: BLE001
            self._pending.fail_all(exc)
        finally:
            self._stop.set()
            self._endpoint_event.set()  # unblock waiters on failure

    def _dispatch(self, event: str | None, data: str) -> None:
        if not data:
            return
        if event == "endpoint":
            self._endpoint = urllib.parse.urljoin(self._url, data)
            self._endpoint_event.set()
            return
        if event is None or event == "message":
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                return
            if isinstance(msg, dict) and "id" in msg:
                self._pending.resolve(msg["id"], msg)

    def _await_endpoint(self, timeout: float) -> str:
        if not self._endpoint_event.wait(timeout=timeout):
            raise MCPError(f"MCP SSE: no endpoint event from {self._url!r}")
        if not self._endpoint:
            raise MCPError(f"MCP SSE stream to {self._url!r} failed")
        return self._endpoint

    def request(self, method: str, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        endpoint = self._await_endpoint(timeout)
        msg_id, slot = self._pending.new()
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                              "params": params or {}}).encode()
        try:
            req = urllib.request.Request(
                endpoint, data=payload,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
            )
            with self._post_lock:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    code = resp.status
            if code not in (200, 201, 202):
                raise MCPError(f"MCP SSE POST got HTTP {code} for {method!r}")
            try:
                message = slot.get(timeout=timeout)
            except queue.Empty:
                raise MCPError(f"MCP request {method!r} timed out after {timeout}s")
            return _check_rpc(message, method)
        finally:
            self._pending.drop(msg_id)

    def notify(self, method: str, params: dict | None = None) -> None:
        endpoint = self._await_endpoint(DEFAULT_TIMEOUT)
        payload = json.dumps({"jsonrpc": "2.0", "method": method,
                              "params": params or {}}).encode()
        req = urllib.request.Request(endpoint, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT):
            pass

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=5)


# ---- client -----------------------------------------------------------------


class MCPClient:
    """One connected MCP server."""

    def __init__(self, transport: Transport, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.transport = transport
        self.timeout = timeout
        self.server_info: dict = {}
        self._connected = False

    def connect(self) -> dict:
        """Run the MCP handshake. Returns the server's initialize result."""
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise MCPError("MCP initialize returned a malformed result")
        self.server_info = result.get("serverInfo", {})
        try:
            self.transport.notify("notifications/initialized", {})
        except MCPError:
            pass  # some servers don't care; not fatal
        self._connected = True
        return result

    @property
    def connected(self) -> bool:
        return self._connected

    def list_tools(self) -> list[dict]:
        result = self.transport.request("tools/list", {}, timeout=self.timeout)
        tools = (result or {}).get("tools", [])
        if not isinstance(tools, list):
            raise MCPError("MCP tools/list returned a malformed result")
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call a remote tool. Returns text, or an "ERROR: ..." string."""
        result = self.transport.request(
            "tools/call", {"name": name, "arguments": arguments or {}},
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise MCPError(f"MCP tools/call {name!r} returned a malformed result")
        parts: list[str] = []
        for item in result.get("content", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(f"[{item.get('type', 'unknown')}] "
                             f"{json.dumps(item, ensure_ascii=False)}")
        text = "\n".join(parts).strip()
        if result.get("isError"):
            return f"ERROR: MCP tool {name!r} failed: {text or 'unknown error'}"
        return text

    def close(self) -> None:
        self._connected = False
        self.transport.close()

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---- config + translation ----------------------------------------------------


def load_mcp_config(path: str | Path | None = None) -> dict:
    """Load ``mcp.json``. Returns ``{"servers": {...}}`` (empty if missing)."""
    p = Path(path).expanduser() if path else DEFAULT_CONFIG
    if not p.exists():
        return {"servers": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPError(f"Could not read MCP config {p}: {exc}")
    if not isinstance(data, dict):
        raise MCPError(f"MCP config {p} must be a JSON object")
    return data


def _build_transport(spec: dict) -> Transport:
    kind = spec.get("transport", "stdio")
    if kind == "stdio":
        command = spec.get("command")
        if not command:
            raise MCPError("stdio MCP server needs a 'command' list")
        if isinstance(command, str):
            command = [command]
        return StdioTransport(command, env=spec.get("env"), cwd=spec.get("cwd"))
    if kind == "sse":
        url = spec.get("url")
        if not url:
            raise MCPError("sse MCP server needs a 'url'")
        return SSETransport(url)
    raise MCPError(f"Unknown MCP transport {kind!r} (want 'stdio' or 'sse')")


def _translate_schema(mcp_schema: Any) -> dict:
    """Coerce an MCP inputSchema into a safe, flat JSON Schema object."""
    if not isinstance(mcp_schema, dict):
        return {"type": "object", "properties": {}}
    schema = dict(mcp_schema)
    schema.setdefault("type", "object")
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def build_mcp_tools(client: MCPClient, server_name: str,
                    allowlist: list[str] | None = None) -> list[Tool]:
    """Translate one connected server's tools into local :class:`Tool`s.

    Tool names are prefixed ``mcp_<server>_<tool>`` to avoid collisions.
    """
    tools: list[Tool] = []
    for spec in client.list_tools():
        name = spec.get("name", "")
        if not name:
            continue
        if allowlist is not None and name not in allowlist:
            continue

        def _call(_tool_name: str = name, **kwargs: Any) -> str:
            return client.call_tool(_tool_name, kwargs)

        tools.append(Tool(
            name=f"mcp_{server_name}_{name}",
            description=f"[MCP:{server_name}] {spec.get('description', name)}",
            json_schema=_translate_schema(spec.get("inputSchema")),
            risk="medium",
            func=_call,
        ))
    return tools


def connect_configured(config_path: str | Path | None = None) -> dict[str, MCPClient]:
    """Connect every server in the config. Failed servers are skipped with a
    stderr warning so one bad server can't take down the agent."""
    import sys

    config = load_mcp_config(config_path)
    clients: dict[str, MCPClient] = {}
    servers = config.get("servers", {})
    if not isinstance(servers, dict):
        raise MCPError("mcp.json 'servers' must be an object")
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            print(f"[mcp] skipping {name!r}: not an object", file=sys.stderr)
            continue
        try:
            timeout = float(spec.get("timeout", DEFAULT_TIMEOUT))
            client = MCPClient(_build_transport(spec), timeout=timeout)
            client.connect()
            clients[name] = client
        except MCPError as exc:
            print(f"[mcp] could not connect to {name!r}: {exc}", file=sys.stderr)
    return clients


def mcp_tools_all(config_path: str | Path | None = None) -> list[Tool]:
    """Connect all configured servers and return their tools as local Tools."""
    config = load_mcp_config(config_path)
    servers = config.get("servers", {})
    tools: list[Tool] = []
    for name, client in connect_configured(config_path).items():
        spec = servers.get(name, {})
        allowlist = spec.get("tools_allowlist")
        tools.extend(build_mcp_tools(client, name, allowlist))
    return tools


__all__ = [
    "MCPError",
    "Transport",
    "StdioTransport",
    "SSETransport",
    "MCPClient",
    "Tool",
    "load_mcp_config",
    "build_mcp_tools",
    "connect_configured",
    "mcp_tools_all",
    "DEFAULT_CONFIG",
]
