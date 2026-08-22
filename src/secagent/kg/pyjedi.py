"""Semantic (type-resolved) Python call extraction via jedi — the "heavy" extractor.

Where ``pycalls`` resolves a call by NAME (and needs stoplists/builtin lists/self checks
to avoid false edges), jedi infers the receiver's type and resolves ``obj.method()`` to the
ACTUAL definition. A call whose definition lives outside the repo (stdlib, third-party,
a builtin) is dropped by construction — no heuristics — because its ``module_path`` is not
under the repo root. The trade is speed: jedi does real inference per call site, so this is
the ``deep=True`` alternative to the fast syntactic path (see ``secagent.kg.extractors``),
not a default. Requires the optional ``python-semantic`` extra (``jedi``); guard a call
with :func:`python_semantic_available` first.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from .store import KnowledgeGraph

if TYPE_CHECKING:
    from ..affordances.store import AffordanceStore

# A resolved definition under one of these is external (installed deps / stdlib stubs).
_EXTERNAL_MARKERS = ("/.venv/", "/site-packages/", "/node_modules/", "/typeshed/", "/lib/python")


def _callee_pos(func: ast.expr) -> tuple[int, int] | None:
    """1-based line, 0-based column of the callee NAME token, for jedi.goto."""
    if isinstance(func, ast.Name):
        return func.lineno, func.col_offset
    if isinstance(func, ast.Attribute) and func.end_lineno is not None and func.end_col_offset:
        return func.end_lineno, func.end_col_offset - len(func.attr)  # start of the attr name
    return None


def _calls(node: ast.AST, caller: str | None, out: list[tuple[str, ast.expr]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _calls(child, child.name, out)
            continue
        if caller is not None and isinstance(child, ast.Call):
            out.append((caller, child.func))
        _calls(child, caller, out)


def python_semantic_available() -> bool:
    """True if jedi is importable, i.e. the semantic Python extractor can run."""
    try:
        import jedi  # noqa: F401
    except ImportError:
        return False
    return True


def extract_python_calls_jedi(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add type-resolved ``calls`` edges for every Python file in ``store``. Returns count."""
    import jedi

    repo_root = Path(store.repo_root)
    project = jedi.Project(str(repo_root))
    added = 0
    for rec in store.file_records():
        if rec.language.lower() != "python":
            continue
        path = repo_root / rec.path
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rec.path)
        except (OSError, SyntaxError, ValueError):
            continue
        script = jedi.Script(source, path=str(path), project=project)
        calls: list[tuple[str, ast.expr]] = []
        _calls(tree, None, calls)
        for caller, func in calls:
            pos = _callee_pos(func)
            if pos is None:
                continue
            try:
                defs = script.goto(*pos, follow_imports=True, follow_builtin_imports=False)
            except Exception:  # noqa: BLE001 — jedi can raise on odd syntax; skip that call
                continue
            for d in defs:
                if d.module_path is None or d.type not in ("function", "class"):
                    continue
                mp = str(d.module_path)
                if any(m in mp for m in _EXTERNAL_MARKERS):
                    continue  # stdlib / third-party — not an internal call
                try:
                    rel = str(Path(mp).relative_to(repo_root))
                except ValueError:
                    continue  # outside the repo
                if not d.name:
                    continue
                cid = kg.add_entity(caller, "SYMBOL", qualifier=rec.path)
                did = kg.add_entity(d.name, "SYMBOL", qualifier=rel)
                if cid != did:
                    kg.add_relation(cid, did, "calls", source=rec.path)
                    added += 1
    kg.commit()
    return added
