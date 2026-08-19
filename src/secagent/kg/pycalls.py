"""Deterministic Python call extraction for the knowledge graph.

The affordance call map is produced only by the clang backend (C/C++) and the heavy
ingest (C#/Rust); the light path used for Python leaves ``calls`` empty, so the KG has
no call chains for its most common target language — and "what calls X" degrades to
file co-location. Python's ``ast`` makes intra/inter-file call resolution cheap and
exact, so this adds ``calls`` edges (function -> function) for Python with no LLM,
restoring the flagship traversal.

Resolution is by simple name against the repo's own function/method symbols — a call to
a name the repo doesn't define (stdlib, third-party, a local variable) is dropped, so
edges connect real repo symbols the projector already created. Name-only resolution can
over-merge two same-named functions, the same modelling-discipline trade-off the
projector already makes for Python's bare-name symbols.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from .store import KnowledgeGraph

if TYPE_CHECKING:
    from ..affordances.store import AffordanceStore


def _call_name(func: ast.expr) -> str | None:
    """The called function's simple name: ``foo()`` -> foo, ``obj.method()`` -> method."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _walk(
    node: ast.AST, caller: str | None, kg: KnowledgeGraph, targets: set[str], source: str
) -> int:
    """Recurse, tracking the enclosing function (the caller); emit an edge for each call
    to a repo-defined name. Module-level calls (caller is None) are skipped — "who calls
    X" is a function-to-function question."""
    added = 0
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            # Descend into the function with it as the new caller.
            added += _walk(child, child.name, kg, targets, source)
            continue
        if caller is not None and isinstance(child, ast.Call):
            callee = _call_name(child.func)
            if callee and callee in targets:
                cid = kg.add_entity(caller, "SYMBOL", source=source)
                did = kg.add_entity(callee, "SYMBOL", source=source)
                kg.add_relation(cid, did, "calls", source=source)
                added += 1
        added += _walk(child, caller, kg, targets, source)
    return added


def extract_python_calls(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add ``calls`` edges for every Python file in ``store``. Returns the edge count.

    Idempotent (edges dedup by (source, target, predicate)); the caller commits via this
    function. Non-Python files and unparseable ones are skipped, never fatal.
    """
    targets = {name for name, _ in store.function_symbol_names()}
    if not targets:
        return 0
    repo_root = Path(store.repo_root)
    added = 0
    for rec in store.file_records():
        if rec.language.lower() != "python":
            continue
        try:
            tree = ast.parse((repo_root / rec.path).read_text(encoding="utf-8"), filename=rec.path)
        except (OSError, SyntaxError, ValueError):
            continue  # unreadable/unparseable file: skip, don't abort the build
        added += _walk(tree, None, kg, targets, rec.path)
    kg.commit()
    return added
