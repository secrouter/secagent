"""Model Context Protocol servers for secagent.

* ``server`` — a minimal, dependency-free stdio JSON-RPC MCP server that exposes a
  ``ToolRegistry``.
* ``affordance_server`` — serves the affordance query tools so external MCP clients
  (IDEs) can browse a repo through secagent's context-reduction layer.
* ``gitlab_harness`` — GitLab REST v4 wrapper exposed as MCP tools for UC100.
"""

from .server import StdioMCPServer

__all__ = ["StdioMCPServer"]
