"""Tool registry and dispatch.

A ``Tool`` couples an LLM-facing schema (``ToolSpec``) with a Python handler. This is
used by secagent's optional MCP surface (``secagent.mcp``). secagent itself does not run an
agent loop — pi (pi.dev) is the runtime; this registry just lets the same affordance
tools be exposed over MCP if desired.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .llm.client import ToolSpec


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any]], str]

    @property
    def name(self) -> str:
        return self.spec.name


class ToolError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], str],
    ) -> None:
        self._tools[name] = Tool(ToolSpec(name, description, parameters), handler)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(self.names())}"
        try:
            result = tool.handler(arguments or {})
        except Exception as exc:  # surface errors to the model rather than crashing
            return f"ERROR calling {name}: {exc}"
        if not isinstance(result, str):
            result = json.dumps(result, default=str)
        return result
