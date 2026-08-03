"""Affordance query tools as a ToolRegistry.

A thin adapter over ``secagent.affordances.queries`` (the single source of truth),
used by the optional MCP server. The same query functions back the
``secagent affordance`` CLI that pi drives via bash.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..llm.tokenizer import TokenCounter
from ..toolkit import ToolRegistry
from . import queries
from .store import AffordanceStore


def _reindex(store: AffordanceStore, settings: Settings | None, llm_summaries: bool) -> str:
    """Re-scan the repo so subsequent answers reflect the current code.

    Every other tool answers from the index, never from disk, so an agent that edits a
    file and then asks about it gets the PREVIOUS version back — presented exactly like
    a current answer. Over MCP there was no way to fix that without dropping to a shell,
    which an MCP client may not even have.

    Heuristic-only by default: an MCP call should not silently spend model tokens, and
    structure/symbols/call map — the things an edit invalidates — need no LLM.
    """
    import json

    from .api import index_repo

    cfg = settings or Settings()
    cfg.affordances.llm_summaries = llm_summaries
    report = index_repo(store.repo_root, cfg)
    # The indexer writes through its own connection; ours would keep serving the old
    # snapshot from an open read transaction otherwise.
    store.reopen()
    return json.dumps({
        "reindexed": str(store.repo_root),
        "files_updated": report.get("updated"),
        "files_unchanged": report.get("skipped"),
        "llm_summaries": llm_summaries,
    })


def build_affordance_tools(
    store: AffordanceStore, budget_tokens: int = 2048, counter: TokenCounter | None = None,
    settings: Settings | None = None,
) -> ToolRegistry:
    max_ctx_files = settings.affordances.max_context_files if settings else 0
    counter = counter or TokenCounter()
    reg = ToolRegistry()

    def _path(args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    reg.add("get_structure", "Return the project structure outline (components, languages). "
                             "Returns an indented text outline, not JSON.",
            {"type": "object", "properties": {}},
            lambda a: queries.structure(store))
    reg.add("get_io_map", "Return the IO map: imports, HTTP endpoints, outbound calls, datastores.",
            {"type": "object", "properties": {}},
            lambda a: queries.io(store))
    reg.add("list_components", "List the repository's components.",
            {"type": "object", "properties": {}},
            lambda a: queries.components(store))
    reg.add("get_file_summary", "Get the affordance summary for one file path.",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            lambda a: queries.summary(store, _path(a)))
    reg.add("find_symbol", "Find functions/classes by (partial) name across the repo.",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            lambda a: queries.find_symbol(store, str(a.get("name", ""))))
    reg.add("search_files", "Rank files by relevance to a query; returns paths + purposes.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            lambda a: queries.search(store, str(a.get("query", "")), counter=counter))
    reg.add("read_file_slice", "Read a bounded slice of a real file (1-indexed inclusive lines).",
            {"type": "object", "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"}, "end": {"type": "integer"}},
             "required": ["path"]},
            lambda a: queries.read_slice(
                store, _path(a), int(a.get("start", 1)), int(a.get("end", a.get("start", 1) + 50))))
    reg.add("context_for", "Assemble a budget-bounded context block relevant to a query.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            lambda a: queries.context(store, str(a.get("query", "")), budget_tokens,
                                      counter, max_files=max_ctx_files))

    # The call map and the rest of the symbol surface. These were previously available
    # only to the pi extension, so every MCP client (Kilo Code, OpenCode)
    # got a materially weaker secagent — with no call map at all, i.e. missing the
    # "what depends on this?" question the toolset exists to answer.
    reg.add("list_functions", "List the functions defined in a file, with signatures and "
                              "(if generated) one-line descriptions.",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"]},
            lambda a: queries.functions(store, _path(a)))
    reg.add("find_callers", "Who calls a function — the reverse call map. Use before "
                            "changing a symbol to gauge blast radius. Rows are "
                            "{path, caller, line, dispatch}; `path` and `line` feed "
                            "read_file_slice directly. `line` is absent on a store "
                            "indexed before call sites were recorded — reindex to get it.",
            {"type": "object", "properties": {"symbol": {"type": "string"}},
             "required": ["symbol"]},
            lambda a: queries.callers(store, str(a.get("symbol", ""))))
    reg.add("get_call_map", "The inter-file call map (file -> file: callees). Pass a path "
                            "to filter to one file's edges. Returns text lines "
                            "`src -> dst: callee, …`, not JSON.",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            lambda a: queries.calls(store, _path(a)))
    reg.add("list_types", "Declared types and their inheritance (bases/interfaces), from "
                          "the heavy analysis backends. Optional name filter.",
            {"type": "object", "properties": {"name": {"type": "string"}}},
            lambda a: queries.types(store, str(a.get("name", ""))))
    reg.add("get_analysis_plan", "Components binned by language plus the secagent tools to "
                                 "run on each. Use first on an unfamiliar project.",
            {"type": "object", "properties": {}},
            lambda a: queries.plan(store))
    reg.add("reindex", "Re-scan the repository after editing files. Every other tool "
                       "answers from the index, so without this they keep returning the "
                       "code as it was when it was last indexed. Fast (no model calls) "
                       "unless llm_summaries is set.",
            {"type": "object", "properties": {
                "llm_summaries": {"type": "boolean",
                                  "description": "Also regenerate LLM file purposes (slow)."}}},
            lambda a: _reindex(store, settings, bool(a.get("llm_summaries", False))))
    return reg
