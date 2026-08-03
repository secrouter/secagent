"""Tool-calling support with a JSON fallback for servers without native tools.

Local model servers vary: vLLM and recent llama.cpp builds support OpenAI-style
``tool_calls``, but many GGUF setups do not. This module lets the loop engine work
either way:

* Native path: ``LLMClient.chat(tools=...)`` returns ``ToolCall`` objects directly.
* Fallback path: we describe the tools in the system prompt and ask the model to
  emit a single JSON object; ``extract_tool_call`` parses it, tolerating the common
  ways small models malform JSON (code fences, trailing prose, single quotes).
"""

from __future__ import annotations

import json
import re

from .client import ToolCall, ToolSpec

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def render_tool_instructions(tools: list[ToolSpec]) -> str:
    """Build the system-prompt section describing the JSON tool protocol."""
    lines = [
        "You can use tools to gather information before answering.",
        "To call a tool, respond with ONLY a single JSON object and nothing else:",
        '  {"tool": "<name>", "arguments": { ... }}',
        "To give your final answer instead, respond with ONLY:",
        '  {"final": "<your answer>"}',
        "",
        "Available tools:",
    ]
    for t in tools:
        props = (t.parameters or {}).get("properties", {})
        arg_names = ", ".join(props.keys()) if props else "(none)"
        lines.append(f"- {t.name}({arg_names}): {t.description}")
    lines.append("")
    lines.append("Call one tool at a time. Do not wrap JSON in markdown unless necessary.")
    return "\n".join(lines)


def _find_json_object(text: str) -> str | None:
    """Locate the first balanced top-level JSON object in free text."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _loads_lenient(blob: str) -> dict | None:
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    # Common small-model malformations: single quotes, trailing commas.
    repaired = re.sub(r",\s*([}\]])", r"\1", blob)
    repaired = repaired.replace("'", '"')
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def extract_tool_call(text: str) -> ToolCall | None:
    """Parse a fallback-protocol tool call from model text, or None.

    Returns a ``ToolCall`` for a ``{"tool": ...}`` object. A ``{"final": ...}``
    object yields ``None`` (the caller treats remaining text as the final answer).
    """
    blob = _find_json_object(text)
    if not blob:
        return None
    obj = _loads_lenient(blob)
    if not isinstance(obj, dict):
        return None
    if "tool" in obj:
        return ToolCall(
            id="fallback",
            name=str(obj["tool"]),
            arguments=obj.get("arguments") or {},
        )
    return None


def extract_final(text: str) -> str | None:
    """Return the final answer if the model emitted ``{"final": ...}``."""
    blob = _find_json_object(text)
    if blob:
        obj = _loads_lenient(blob)
        if isinstance(obj, dict) and "final" in obj:
            return str(obj["final"])
    return None
