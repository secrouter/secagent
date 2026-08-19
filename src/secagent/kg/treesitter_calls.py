"""Deterministic Go / TypeScript / JavaScript call extraction for the KG (tree-sitter).

The affordance light path leaves Go and TS with no call edges (they fall to the regex
symbol backend — there is no Go/TS affordance backend), so "what calls X / what breaks"
could not be answered for them. This adds ``calls`` (function -> function) edges via the
tree-sitter grammars — no LLM, no language toolchain — mirroring ``pycalls`` for Python
and ``affordances.rust_ast`` for Rust.

Degrades gracefully: if a grammar is not installed (the ``go`` / ``typescript`` extras),
that language is silently skipped and the build still succeeds without its call edges.
Resolution is by simple name against the repo's own function/method definitions (calls to
stdlib/third-party names are dropped), the same discipline as ``pycalls``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .store import KnowledgeGraph

if TYPE_CHECKING:
    from ..affordances.store import AffordanceStore

# File extension -> tree-sitter language key. JS/JSX use the tsx grammar (a superset that
# parses JSX and plain JS).
_EXT_LANG = {
    ".go": "go", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx", ".js": "tsx", ".jsx": "tsx", ".mjs": "tsx", ".cjs": "tsx",
}

# Node types that introduce a named callable (an enclosing scope for calls), per grammar.
_DEF_TYPES = {
    "go": frozenset({"function_declaration", "method_declaration"}),
    "typescript": frozenset({"function_declaration", "method_definition"}),
    "tsx": frozenset({"function_declaration", "method_definition"}),
}
# TS/JS also write functions as `const f = () => {…}` / `= function(){…}`.
_TS_FUNC_VALUES = frozenset({"arrow_function", "function", "function_expression"})


def _language(key: str):
    from tree_sitter import Language

    if key == "go":
        import tree_sitter_go

        return Language(tree_sitter_go.language())
    import tree_sitter_typescript as ts_ts

    # The grammar package ships no type stubs; these functions exist at runtime.
    fn = ts_ts.language_tsx if key == "tsx" else ts_ts.language_typescript
    return Language(fn())


@lru_cache(maxsize=4)
def _parser(key: str):
    from tree_sitter import Parser

    lang = _language(key)
    try:  # tree-sitter >= 0.22
        return Parser(lang)
    except TypeError:  # older API
        parser = Parser()
        parser.language = lang
        return parser


@lru_cache(maxsize=4)
def _available(key: str) -> bool:
    try:
        _parser(key)
        return True
    except Exception:  # noqa: BLE001 — missing grammar => skip this language
        return False


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _callee_name(fn) -> str:
    """The spelled name of a call's function expression (Go + TS/JS forms)."""
    if fn is None:
        return ""
    if fn.type == "identifier":
        return _text(fn)
    if fn.type == "selector_expression":  # Go: receiver.Method()
        return _text(fn.child_by_field_name("field"))
    if fn.type == "member_expression":  # TS/JS: obj.method()
        return _text(fn.child_by_field_name("property"))
    return ""


def _walk(key: str, root, defs: set[str], calls: list[tuple[str, str, str]], rel: str) -> None:
    """Iterative DFS carrying the enclosing callable's name so calls are attributed to it."""
    def_types = _DEF_TYPES[key]
    stack = [(root, "")]
    while stack:
        node, enclosing = stack.pop()
        enc = enclosing
        if node.type in def_types:
            name = _text(node.child_by_field_name("name"))
            if name:
                defs.add(name)
                enc = name
        elif node.type == "variable_declarator":  # const f = () => {…}
            value = node.child_by_field_name("value")
            if value is not None and value.type in _TS_FUNC_VALUES:
                name = _text(node.child_by_field_name("name"))
                if name:
                    defs.add(name)
                    enc = name
        elif node.type == "call_expression":
            callee = _callee_name(node.child_by_field_name("function"))
            if callee:
                calls.append((enclosing, callee, rel))
        for child in node.children:
            stack.append((child, enc))


def extract_treesitter_calls(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add Go/TS/JS ``calls`` edges to ``kg``; returns the edge count (0 if no grammars)."""
    repo_root = Path(store.repo_root)
    by_lang: dict[str, list[str]] = {}
    for rec in store.file_records():
        key = _EXT_LANG.get(Path(rec.path).suffix.lower())
        if key:
            by_lang.setdefault(key, []).append(rec.path)
    if not by_lang:
        return 0

    defs: set[str] = set()
    pending: list[tuple[str, str, str]] = []
    for key, files in by_lang.items():
        if not _available(key):
            continue
        parser = _parser(key)
        for rel in files:
            try:
                src = (repo_root / rel).read_bytes()
            except OSError:
                continue
            _walk(key, parser.parse(src).root_node, defs, pending, rel)

    # Emit only after every file's defs are known, so a call to a function defined in
    # another file still resolves. Drop calls to names the repo doesn't define.
    added = 0
    for caller, callee, rel in pending:
        if caller and callee in defs:
            # `rel` is the call site, not necessarily the definition of caller/callee —
            # record it on the relation only; the projector owns entity `source`.
            cid = kg.add_entity(caller, "SYMBOL")
            did = kg.add_entity(callee, "SYMBOL")
            kg.add_relation(cid, did, "calls", source=rel)
            added += 1
    kg.commit()
    return added
