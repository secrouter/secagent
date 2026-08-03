"""Expose the affordance query tools over MCP (stdio)."""

from __future__ import annotations

from pathlib import Path

from ..affordances.store import AffordanceStore
from ..affordances.tools import build_affordance_tools
from ..audit import get_audit_logger
from ..config import Settings
from ..llm.tokenizer import TokenCounter
from .server import StdioMCPServer


def build_server(path: str | Path, settings: Settings) -> tuple[StdioMCPServer, AffordanceStore]:
    store = AffordanceStore(path, settings.affordances.store_dir)
    counter = TokenCounter()
    tools = build_affordance_tools(
        store, settings.llm.context_budget_tokens, counter, settings=settings
    )
    server = StdioMCPServer(
        tools, server_name="secagent-affordances", audit=get_audit_logger(settings)
    )
    return server, store


def serve_stdio(path: str | Path, settings: Settings) -> None:
    server, store = build_server(path, settings)
    try:
        server.serve()
    finally:
        store.close()
