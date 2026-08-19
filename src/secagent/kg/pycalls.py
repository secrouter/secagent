"""Deterministic Python call extraction for the knowledge graph.

The affordance light path leaves Python ``calls`` empty, so this adds ``calls`` edges
(function -> function) from the ast, with no LLM. Callees are resolved to their DEFINING
file so the edge lands on the right node: two different functions named ``_walk`` in two
files are two entities, and a call resolves to the one actually defined (same-file first,
else the unique definition, else a deterministic pick for a genuinely ambiguous name).
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


def _walk(node: ast.AST, caller: str | None, calls: list[tuple[str, str]]) -> None:
    """Collect ``(caller, callee)`` pairs, tracking the enclosing function as the caller.
    Module-level calls (caller is None) are skipped — "who calls X" is function-to-function."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _walk(child, child.name, calls)
            continue
        if caller is not None and isinstance(child, ast.Call):
            callee = _call_name(child.func)
            if callee:
                calls.append((caller, callee))
        _walk(child, caller, calls)


def _resolve(callee: str, caller_file: str, def_files: dict[str, list[str]]) -> str | None:
    """The defining file to attribute a call to ``callee`` from ``caller_file``.

    Same-file definition wins (the common local-helper call); otherwise the unique
    definition; otherwise a deterministic pick for a name defined in several other files
    (genuinely ambiguous without type information). None if the repo defines no such name.
    """
    files = def_files.get(callee)
    if not files:
        return None
    if caller_file in files:
        return caller_file
    return files[0] if len(files) == 1 else sorted(files)[0]


def extract_python_calls(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add resolved ``calls`` edges for every Python file in ``store``. Returns the count."""
    def_files: dict[str, list[str]] = {}
    for name, path in store.function_symbol_names():
        def_files.setdefault(name, []).append(path)
    if not def_files:
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
        calls: list[tuple[str, str]] = []
        _walk(tree, None, calls)
        for caller, callee in calls:
            if callee not in def_files:
                continue  # a call to something the repo doesn't define (stdlib, third-party)
            callee_file = _resolve(callee, rec.path, def_files)
            if callee_file is None:
                continue
            cid = kg.add_entity(caller, "SYMBOL", qualifier=rec.path)
            did = kg.add_entity(callee, "SYMBOL", qualifier=callee_file)
            if cid != did:  # drop self-loops (recursion / a namesake resolved to itself)
                kg.add_relation(cid, did, "calls", source=rec.path)
                added += 1
    kg.commit()
    return added
