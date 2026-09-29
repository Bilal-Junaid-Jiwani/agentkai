"""Tests for the MCP client. All transports are faked — no network, no processes."""
import json

import pytest

from agentkai.mcp import (
    MCPClient,
    MCPError,
    Transport,
    _translate_schema,
    build_mcp_tools,
    load_mcp_config,
)
from agentkai.tools import ApprovalRequired, PermissionDenied, Tool


class FakeTransport(Transport):
    """Canned JSON-RPC responder."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []
        self.notifies: list[tuple[str, dict]] = []
        self.closed = False

    def request(self, method, params=None, timeout=30):
        self.requests.append((method, params or {}))
        if method not in self.responses:
            raise MCPError(f"unexpected method {method!r}")
        resp = self.responses[method]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def notify(self, method, params=None):
        self.notifies.append((method, params or {}))

    def close(self):
        self.closed = True


def make_client(**overrides):
    responses = {
        "initialize": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake", "version": "1.0"},
        },
        "tools/list": {
            "tools": [
                {
                    "name": "read",
                    "description": "Read a thing",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {"name": "write", "description": "Write a thing",
                 "inputSchema": {"type": "object"}},
            ]
        },
        "tools/call": {"content": [{"type": "text", "text": "ok-result"}]},
    }
    responses.update(overrides)
    return MCPClient(FakeTransport(responses))


def test_connect_handshake():
    client = make_client()
    result = client.connect()
    assert client.connected
    assert client.server_info["name"] == "fake"
    assert result["protocolVersion"] == "2024-11-05"
    transport = client.transport
    assert transport.notifies[0][0] == "notifications/initialized"


def test_connect_bad_result_raises():
    client = make_client(**{"initialize": "not-a-dict"})
    with pytest.raises(MCPError):
        client.connect()


def test_list_tools():
    client = make_client()
    client.connect()
    tools = client.list_tools()
    assert [t["name"] for t in tools] == ["read", "write"]


def test_call_tool_text():
    client = make_client()
    client.connect()
    assert client.call_tool("read", {"path": "x"}) == "ok-result"
    method, params = client.transport.requests[-1]
    assert method == "tools/call"
    assert params == {"name": "read", "arguments": {"path": "x"}}


def test_call_tool_multiple_content_parts():
    client = make_client(**{"tools/call": {
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}})
    client.connect()
    assert client.call_tool("read", {}) == "a\nb"


def test_call_tool_is_error_returns_error_string():
    client = make_client(**{"tools/call": {
        "content": [{"type": "text", "text": "boom"}],
        "isError": True}})
    client.connect()
    result = client.call_tool("read", {})
    assert result.startswith("ERROR:")
    assert "boom" in result


def test_transport_timeout_surfaces_as_mcperror():
    client = make_client(**{"tools/list": MCPError("timed out")})
    client.connect()
    with pytest.raises(MCPError):
        client.list_tools()


# ---- schema translation -------------------------------------------------------


def test_translate_schema_passthrough():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert _translate_schema(schema)["properties"] == {"a": {"type": "string"}}


def test_translate_schema_coerces_garbage():
    assert _translate_schema(None) == {"type": "object", "properties": {}}
    assert _translate_schema("nope") == {"type": "object", "properties": {}}
    out = _translate_schema({"type": "object", "properties": None})
    assert out["properties"] == {}


def test_build_mcp_tools():
    client = make_client()
    client.connect()
    tools = build_mcp_tools(client, "fs")
    assert len(tools) == 2
    read_tool = tools[0]
    assert isinstance(read_tool, Tool)
    assert read_tool.name == "mcp_fs_read"
    assert read_tool.risk == "medium"
    assert "Read a thing" in read_tool.description
    assert read_tool.json_schema["properties"]["path"]["type"] == "string"
    # required fields of the Tool dataclass are all present
    assert read_tool.name and read_tool.description
    assert isinstance(read_tool.json_schema, dict) and callable(read_tool.func)


def test_build_mcp_tools_allowlist():
    client = make_client()
    client.connect()
    tools = build_mcp_tools(client, "fs", allowlist=["write"])
    assert [t.name for t in tools] == ["mcp_fs_write"]


def test_mcp_tool_run_and_gate():
    client = make_client()
    client.connect()
    tool = build_mcp_tools(client, "fs")[0]
    assert tool.run(path="x") == "ok-result"
    with pytest.raises(PermissionDenied):
        tool.run(_gate=lambda a: "deny", path="x")
    with pytest.raises(ApprovalRequired):
        tool.run(_gate=lambda a: "ask", path="x")
    # gate sees the prefixed name and medium risk
    seen = {}
    tool.run(_gate=lambda a: seen.update(a) or "allow", path="x")
    assert seen["tool"] == "mcp_fs_read"
    assert seen["risk"] == "medium"


def test_mcp_tool_protocol_failure_becomes_error_string():
    client = make_client(**{"tools/call": MCPError("server exploded")})
    client.connect()
    tool = build_mcp_tools(client, "fs")[0]
    result = tool.run(path="x")
    assert result.startswith("ERROR:")


def test_load_mcp_config_missing(tmp_path):
    assert load_mcp_config(tmp_path / "nope.json") == {"servers": {}}


def test_load_mcp_config_valid(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"a": {"transport": "stdio",
                                                 "command": ["true"]}}}))
    assert load_mcp_config(cfg)["servers"]["a"]["transport"] == "stdio"


def test_client_context_manager_closes():
    client = make_client()
    with client:
        assert client.connected
    assert client.transport.closed
