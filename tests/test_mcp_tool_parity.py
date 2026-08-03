"""The MCP surface must expose the same affordances as the pi extension.

Any MCP client — Kilo Code, OpenCode — reaches secagent only through this
registry. It used to carry 8 of the 13 tools, missing the entire call map: `find_callers`
is the "what depends on this?" question the toolset exists to answer, and MCP users could
not ask it at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from secagent.affordances.store import AffordanceStore
from secagent.affordances.tools import build_affordance_tools
from secagent.config import Settings
from secagent.mcp.affordance_server import build_server

PI_EXTENSION = Path(__file__).resolve().parents[1] / "pi" / "extensions" / "secagent.ts"

# MCP names are verb_noun; the pi extension uses secagent_<affordance>. Map the concepts.
_EQUIVALENT = {
    "structure": "get_structure", "io_map": "get_io_map", "components": "list_components",
    "search": "search_files", "file_summary": "get_file_summary",
    "find_symbol": "find_symbol", "functions": "list_functions",
    "calls": "get_call_map", "callers": "find_callers", "types": "list_types",
    "plan": "get_analysis_plan", "context": "context_for",
    "read_slice": "read_file_slice",
}


def _tools(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        return {t.name for t in build_affordance_tools(store).specs()}
    finally:
        store.close()


def test_mcp_exposes_every_pi_affordance(tmp_path):
    """Guards the parity gap: pi users and MCP users must get the same capabilities."""
    pi_names = set(re.findall(r'name: "secagent_([a-z_]+)"', PI_EXTENSION.read_text()))
    assert pi_names, "could not read the pi extension's tool names"
    mcp_names = _tools(tmp_path)
    missing = {p: _EQUIVALENT.get(p) for p in pi_names
               if _EQUIVALENT.get(p) not in mcp_names}
    assert not missing, f"MCP surface is missing: {missing}"


def test_call_map_tools_are_reachable_over_mcp(tmp_path):
    """The specific regression: no call map at all for MCP clients."""
    names = _tools(tmp_path)
    assert {"find_callers", "get_call_map", "list_functions"} <= names


def test_server_advertises_the_tools_with_schemas(tmp_path):
    """tools/list is what an editor reads to build its tool palette."""
    server, store = build_server(tmp_path, Settings())
    try:
        resp = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = resp["result"]["tools"]
        # Compared against the registry, not a literal: the point is that every tool
        # built is actually advertised. A hardcoded count only measures whether someone
        # remembered to bump it.
        assert {t["name"] for t in tools} == _tools(tmp_path)
        for t in tools:
            assert t["name"] and t["description"]
            assert t["inputSchema"]["type"] == "object"
        by_name = {t["name"]: t for t in tools}
        # Required args must be declared, or a client cannot prompt for them.
        assert by_name["find_callers"]["inputSchema"]["required"] == ["symbol"]
        assert by_name["list_functions"]["inputSchema"]["required"] == ["path"]
    finally:
        store.close()


def test_tool_call_round_trips(tmp_path):
    """An empty repo still answers structurally rather than erroring."""
    server, store = build_server(tmp_path, Settings())
    try:
        resp = server.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "find_callers", "arguments": {"symbol": "nope"}}})
        text = resp["result"]["content"][0]["text"]
        assert "note" in json.loads(text)          # the JSON not-found shape
    finally:
        store.close()
