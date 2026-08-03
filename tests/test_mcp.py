"""Tests for the affordance tools and the minimal MCP stdio server."""

from __future__ import annotations

import io
import json
from pathlib import Path

from secagent.affordances.api import index_repo
from secagent.affordances.store import AffordanceStore
from secagent.affordances.tools import build_affordance_tools
from secagent.config import Settings
from secagent.mcp.affordance_server import build_server
from secagent.mcp.server import StdioMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _indexed_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    index_repo(FIXTURE, s)
    return s


def test_affordance_tools_cover_engine_surface(tmp_path):
    s = _indexed_settings(tmp_path)
    store = AffordanceStore(FIXTURE, s.affordances.store_dir)
    try:
        tools = build_affordance_tools(store)
        assert {"get_structure", "get_io_map", "find_symbol", "read_file_slice"} <= set(
            tools.names()
        )
        assert "Python" in tools.call("get_structure", {})
        syms = json.loads(tools.call("find_symbol", {"name": "get_user"}))
        assert any(x["name"] == "get_user" for x in syms)
        sl = tools.call("read_file_slice", {"path": "services/api/db.py", "start": 1, "end": 3})
        assert "1:" in sl
    finally:
        store.close()


def test_read_file_slice_blocks_traversal(tmp_path):
    s = _indexed_settings(tmp_path)
    store = AffordanceStore(FIXTURE, s.affordances.store_dir)
    try:
        tools = build_affordance_tools(store)
        out = tools.call("read_file_slice", {"path": "../../../../etc/passwd"})
        assert out.startswith("ERROR")
    finally:
        store.close()


def test_mcp_initialize_list_call(tmp_path):
    s = _indexed_settings(tmp_path)
    server, store = build_server(FIXTURE, s)
    try:
        init = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init["result"]["serverInfo"]["name"] == "secagent-affordances"

        listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "get_structure" in names

        called = server.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_structure", "arguments": {}},
        })
        assert called["result"]["isError"] is False
        assert "Python" in called["result"]["content"][0]["text"]
    finally:
        store.close()


def test_mcp_notification_returns_none():
    from secagent.toolkit import ToolRegistry

    server = StdioMCPServer(ToolRegistry())
    out = server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert out is None


def test_mcp_serve_loop_roundtrip(tmp_path):
    s = _indexed_settings(tmp_path)
    server, store = build_server(FIXTURE, s)
    try:
        requests = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "list_components", "arguments": {}}}),
        ])
        stdin = io.StringIO(requests + "\n")
        stdout = io.StringIO()
        server.serve(stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0]["id"] == 1
        assert "components" in lines[1]["result"]["content"][0]["text"] or \
            lines[1]["result"]["content"][0]["text"].startswith("[")
    finally:
        store.close()
