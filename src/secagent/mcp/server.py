"""A minimal stdio MCP server.

Implements just enough of the Model Context Protocol (JSON-RPC 2.0 over
newline-delimited stdio) to expose a ``ToolRegistry``: ``initialize``,
``tools/list``, ``tools/call``, and ``ping``. Kept dependency-free on purpose so the
container image carries no extra crypto/runtime surface for FIPS review.

Protocol revision advertised: 2025-06-18.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from ..toolkit import ToolRegistry

PROTOCOL_VERSION = "2025-06-18"


class StdioMCPServer:
    def __init__(self, tools: ToolRegistry, server_name: str = "secagent",
                 audit=None) -> None:
        self.tools = tools
        self.server_name = server_name
        self.audit = audit  # optional AuditLogger; records tools/call when set

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC message; return a response, or None for notifications."""
        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications (no id) get no response.
        if msg_id is None and method and method.startswith("notifications/"):
            return None

        if method == "initialize":
            return _ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": "0.1.0"},
            })
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": self._tool_list()})
        if method == "tools/call":
            return self._call(msg_id, msg.get("params") or {})

        if msg_id is None:
            return None
        return _err(msg_id, -32601, f"method not found: {method}")

    def _tool_list(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "description": s.description, "inputSchema": s.parameters}
            for s in self.tools.specs()
        ]

    def _call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in self.tools:
            return _err(msg_id, -32602, f"unknown tool: {name}")
        result = self.tools.call(name, arguments)
        is_error = result.startswith("ERROR")
        if self.audit is not None:
            self.audit.record(
                "mcp_call",
                target={"server": self.server_name, "tool": name,
                        "args": list((arguments or {}).keys())},
                outcome="error" if is_error else "ok",
            )
        return _ok(msg_id, {
            "content": [{"type": "text", "text": result}],
            "isError": is_error,
        })

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """Run the read/dispatch/write loop until EOF."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _err(None, -32700, "parse error"))
                continue
            response = self.handle_message(msg)
            if response is not None:
                _write(stdout, response)


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()
