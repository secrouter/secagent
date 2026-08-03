"""Expose the GitLab harness as an MCP (stdio) server."""

from __future__ import annotations

from ..audit import get_audit_logger
from ..config import Settings
from .gitlab_harness import GitLabClient, build_gitlab_tools
from .server import StdioMCPServer


def build_server(settings: Settings) -> tuple[StdioMCPServer, GitLabClient]:
    client = GitLabClient(settings.gitlab)
    tools = build_gitlab_tools(client)
    server = StdioMCPServer(
        tools, server_name="secagent-gitlab", audit=get_audit_logger(settings)
    )
    return server, client


def serve_stdio(settings: Settings) -> None:
    server, client = build_server(settings)
    try:
        server.serve()
    finally:
        client.close()
