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


# Method names that are overwhelmingly stdlib/container/builtin, not project functions.
# For a MEMBER call (``obj.get()``) to one of these that has no same-file definition, the
# receiver is almost certainly an external type (a dict, a file, a Map, a *os.File), so
# resolving the bare name to an internal namesake would be a false edge. A same-file
# definition still resolves (``self.close()`` where the class defines ``close``), and a
# plain ``close()`` identifier call is never affected. Kept deliberately narrow: names
# that are commonly meaningful project methods (start/run/new/add/handle/...) are excluded.
_GENERIC_METHODS = frozenset([
    "get", "set", "has", "close", "open", "read", "write", "keys", "values", "items",
    "entries", "next", "push", "pop", "shift", "unshift", "append", "extend", "insert",
    "clear", "map", "filter", "reduce", "foreach", "then", "catch", "finally", "tostring",
    "valueof", "commit", "flush", "rollback", "key", "value", "length", "size", "count",
    "len", "cap", "join", "split", "trim", "slice", "splice", "concat", "includes",
    "indexof", "lock", "unlock", "scan", "fetchall", "fetchone", "exec", "query",
    "prepare", "bind",
])


def _call_name(func: ast.expr) -> str | None:
    """The called function's simple name: ``foo()`` -> foo, ``obj.method()`` -> method."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _walk(node: ast.AST, caller: str | None, calls: list[tuple[str, str, bool, bool]]) -> None:
    """Collect ``(caller, callee, is_member, is_self)`` tuples, tracking the enclosing
    function as the caller. Module-level calls (caller is None) are skipped."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _walk(child, child.name, calls)
            continue
        if caller is not None and isinstance(child, ast.Call):
            func = child.func
            callee = _call_name(func)
            if callee:
                is_member = isinstance(func, ast.Attribute)
                is_self = (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("self", "cls")
                )
                calls.append((caller, callee, is_member, is_self))
        _walk(child, caller, calls)


def _resolve(
    callee: str, caller_file: str, def_files: dict[str, list[str]],
    is_member: bool = False, is_self: bool = False,
) -> str | None:
    """The defining file to attribute a call to ``callee`` from ``caller_file``.

    A member call to a generic stdlib-ish method name (``obj.get()``, ``f.Close()``)
    resolves to None UNLESS the receiver is ``self``/``this`` — the receiver is otherwise
    almost certainly an external type (a dict, a file, a Map), so binding it to an internal
    namesake would be a false edge. ``self.close()`` is definitely the class's own method,
    so it resolves. Then: the same-file definition (the common local-helper call), else the
    unique definition, else a deterministic pick for a genuinely ambiguous cross-file name.
    None if the repo defines no such name at all.
    """
    files = def_files.get(callee)
    if not files:
        return None
    if is_member and not is_self and callee.lower() in _GENERIC_METHODS:
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
        calls: list[tuple[str, str, bool, bool]] = []
        _walk(tree, None, calls)
        for caller, callee, is_member, is_self in calls:
            if callee not in def_files:
                continue  # a call to something the repo doesn't define (stdlib, third-party)
            callee_file = _resolve(callee, rec.path, def_files, is_member, is_self)
            if callee_file is None:
                continue
            cid = kg.add_entity(caller, "SYMBOL", qualifier=rec.path)
            did = kg.add_entity(callee, "SYMBOL", qualifier=callee_file)
            if cid != did:  # drop self-loops (recursion / a namesake resolved to itself)
                kg.add_relation(cid, did, "calls", source=rec.path)
                added += 1
    kg.commit()
    return added
