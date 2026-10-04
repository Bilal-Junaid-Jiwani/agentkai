"""Tests for the safe tool registry. No network access."""
import os

import pytest

from agentkai.tools import (
    ApprovalRequired,
    PermissionDenied,
    Registry,
    Tool,
    default_registry,
)


@pytest.fixture()
def reg(tmp_path):
    return default_registry(roots=[tmp_path])


def test_tool_dataclass_fields(reg):
    tool = reg.get("exec")
    assert isinstance(tool, Tool)
    assert tool.name == "exec"
    assert isinstance(tool.description, str) and tool.description
    assert isinstance(tool.json_schema, dict)
    assert tool.json_schema["type"] == "object"
    assert tool.risk in ("low", "medium", "high")
    assert callable(tool.func)
    # every registered tool declares a risk
    for name in reg.names():
        assert reg.get(name).risk in ("low", "medium", "high"), name


def test_to_openai_schema(reg):
    schema = reg.get("read_file").to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "read_file"
    assert "parameters" in schema["function"]


def test_registry_schemas_roundtrip(reg):
    assert {s["function"]["name"] for s in reg.schemas()} == set(reg.names())


# ---- path traversal --------------------------------------------------------


def test_read_file_blocks_traversal(reg, tmp_path):
    outside = tmp_path.parent / "outside_note.txt"
    outside.write_text("classified-content")
    result = reg.get("read_file").run(path="../outside_note.txt")
    assert isinstance(result, str) and result.startswith("ERROR:")
    assert "classified-content" not in result


def test_write_file_blocks_absolute_escape(reg, tmp_path):
    result = reg.get("write_file").run(path="/etc/agentkai-pwned", content="x")
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_read_file_blocks_symlink_escape(reg, tmp_path):
    outside = tmp_path.parent / "real_secret.txt"
    outside.write_text("topsecret")
    link = tmp_path / "sneaky.txt"
    os.symlink(outside, link)
    result = reg.get("read_file").run(path="sneaky.txt")
    assert isinstance(result, str) and result.startswith("ERROR:")
    assert "topsecret" not in result


def test_write_file_blocks_symlink_escape(reg, tmp_path):
    outside = tmp_path.parent / "victim.txt"
    outside.write_text("original")
    link = tmp_path / "sneaky.txt"
    os.symlink(outside, link)
    result = reg.get("write_file").run(path="sneaky.txt", content="pwned")
    assert isinstance(result, str) and result.startswith("ERROR:")
    assert outside.read_text() == "original"


def test_read_write_inside_roots_ok(reg, tmp_path):
    w = reg.get("write_file").run(path="sub/note.txt", content="hello")
    assert w["ok"] is True
    r = reg.get("read_file").run(path="sub/note.txt")
    assert r["lines"] == ["hello"]


def test_list_dir_blocks_escape(reg):
    result = reg.get("list_dir").run(path="/etc")
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_list_dir_ok(reg, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    result = reg.get("list_dir").run(path=".")
    names = {e["name"] for e in result["entries"]}
    assert "a.txt" in names


# ---- exec ------------------------------------------------------------------


def test_exec_timeout(reg):
    result = reg.get("exec").run(command="sleep 5", timeout=1)
    assert result["exit_code"] == -1
    assert "timed out" in result["output"]


def test_exec_blocklist(reg):
    for cmd in ("rm -rf /", "rm -rf /*", ":(){ :|:& };:", "mkfs.ext4 /dev/sda1",
                "dd if=/dev/zero of=/dev/sda", "shutdown now"):
        result = reg.get("exec").run(command=cmd)
        assert isinstance(result, str), cmd
        assert result.startswith("ERROR:") and "blocked" in result, cmd


def test_exec_allows_legit_rm_rf(reg, tmp_path):
    target = tmp_path / "build"
    target.mkdir()
    result = reg.get("exec").run(command="rm -rf ./build && echo done")
    assert result["exit_code"] == 0
    assert "done" in result["output"]


def test_exec_cwd_escape_blocked(reg):
    result = reg.get("exec").run(command="pwd", workdir="/etc")
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_exec_basic(reg):
    result = reg.get("exec").run(command="echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


# ---- permission gate --------------------------------------------------------


def test_gate_allow(reg):
    tool = reg.get("read_file")
    seen = {}

    def gate(action):
        seen.update(action)
        return "allow"

    reg.get("exec").run(_gate=gate, command="echo hi")
    assert seen["tool"] == "exec"
    assert seen["args"]["command"] == "echo hi"
    assert seen["risk"] == "high"


def test_gate_deny_raises(reg):
    with pytest.raises(PermissionDenied):
        reg.get("write_file").run(_gate=lambda a: "deny",
                                  path="x.txt", content="y")


def test_gate_ask_raises_approval_required(reg):
    with pytest.raises(ApprovalRequired):
        reg.get("exec").run(_gate=lambda a: "ask", command="echo hi")


def test_tool_errors_are_strings_not_raises(reg):
    result = reg.get("read_file").run(path="does-not-exist.txt")
    assert isinstance(result, str) and result.startswith("ERROR:")


# ---- fetch_url (validation only, no network) --------------------------------


def test_fetch_url_rejects_scheme(reg):
    result = reg.get("fetch_url").run(url="ftp://example.com/x")
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_fetch_url_rejects_embedded_credentials(reg):
    result = reg.get("fetch_url").run(url="https://user:pass@example.com/")
    assert isinstance(result, str) and result.startswith("ERROR:")
    assert "pass" not in result  # credentials never leak into the message


def test_fetch_url_rejects_private_host(reg, monkeypatch):
    import agentkai.tools as tools_mod

    def fake_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("192.168.1.5", 0))]

    monkeypatch.setattr(tools_mod.socket, "getaddrinfo", fake_getaddrinfo)
    result = reg.get("fetch_url").run(url="http://example.com/")
    assert isinstance(result, str) and result.startswith("ERROR:")
    assert "non-public" in result


def test_fetch_url_rejects_unresolvable(reg, monkeypatch):
    import agentkai.tools as tools_mod
    import socket as socket_mod

    def boom(host, port, *a, **k):
        raise socket_mod.gaierror("nope")

    monkeypatch.setattr(tools_mod.socket, "getaddrinfo", boom)
    result = reg.get("fetch_url").run(url="http://nonexistent.invalid/")
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_registry_describe_exposes_public_metadata(reg):
    desc = reg.describe()
    assert isinstance(desc, list) and desc
    for entry in desc:
        assert set(entry) == {"name", "description", "risk"}
        assert entry["risk"] in ("low", "medium", "high")
    # names line up with the registry itself
    assert {e["name"] for e in desc} == set(reg.names())
    # no callables leak into the listing
    assert not any(callable(e["description"]) for e in desc)
