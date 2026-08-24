"""Semantic (type-resolved) TypeScript/JavaScript call extraction — the "heavy"
counterpart to ``treesitter_calls`` for TS/JS, mirroring ``pyjedi`` for Python.

Python has no TypeScript type checker, so this shells out to a small Node helper
(``tools/kg-tscalls/extract.mjs``) that drives the real ``typescript`` compiler API:
``ts.createProgram`` over the repo's own file list (using its ``tsconfig.json`` for
compiler options when present), then ``checker.getResolvedSignature`` on every call
expression. That resolves the call against the RECEIVER'S INFERRED TYPE, so
``obj.method()`` lands on the method actually reachable on ``obj``'s type — not just any
same-named method in the repo, which is the best the syntactic tree-sitter extractor can
do. A call whose resolved declaration lives outside the repo (lib.d.ts, node_modules, an
ambient/bodiless declaration) is dropped by construction, the same discipline as jedi's
``module_path`` check for Python.

The helper also tags each edge's ``kind``: a call resolving to a method that some
subclass in the repo overrides is potentially-polymorphic dispatch (the checker's static
resolution lands on the base declaration, but the receiver could be any subtype at
runtime), so it comes back as ``"virtual"`` and is mapped below to the graph's honest
``may_call`` predicate — the same ``direct``/``virtual`` -> ``calls``/``may_call``
convention :mod:`gocalls_semantic` uses for Go's interface/virtual dispatch.

Requires ``node`` on PATH and the ``typescript`` package installed under the helper's own
directory (``tools/kg-tscalls/node_modules``, via ``npm install`` there — not a Python
extra, since this is a Node dependency). Guard a call with :func:`ts_semantic_available`
first; degrades to 0 edges / unavailable on any missing dependency or helper failure,
never raises.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .store import KnowledgeGraph

if TYPE_CHECKING:
    from ..affordances.store import AffordanceStore

_LANGS = ("typescript", "javascript")

# tools/kg-tscalls/, resolved relative to this file (…/src/secagent/kg/tscalls_semantic.py
# -> repo root is four levels up), not the target repo being analyzed.
_TOOL_DIR = Path(__file__).resolve().parents[3] / "tools" / "kg-tscalls"
_HELPER = _TOOL_DIR / "extract.mjs"

# The helper does real type-checking work per call site; large repos need real time.
_TIMEOUT_S = 300

# The helper's edge "kind" -> the graph predicate (mirrors gocalls_semantic.py's
# _PREDICATE: a "direct" call is unconditional; "virtual" — resolved to a method some
# subclass overrides — is potentially-one-of-several polymorphic dispatch, so it becomes
# the honest ``may_call``).
_PREDICATE = {"direct": "calls", "virtual": "may_call"}


@lru_cache(maxsize=1)
def ts_semantic_available() -> bool:
    """True if ``node`` is on PATH, the helper script exists, and its vendored
    ``typescript`` package actually loads — i.e. the semantic TS/JS extractor can run."""
    if shutil.which("node") is None or not _HELPER.is_file():
        return False
    try:
        proc = subprocess.run(
            ["node", "-e", "require('typescript')"],
            cwd=str(_TOOL_DIR), capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def extract_ts_semantic(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add type-resolved TS/JS ``calls``/``may_call`` edges to ``kg``. Returns the edge
    count (0 if the helper is unavailable, finds nothing, or fails for any reason)."""
    repo_root = Path(store.repo_root)
    files = [rec.path for rec in store.file_records() if rec.language.lower() in _LANGS]
    if not files:
        return 0

    job = json.dumps({"repoRoot": str(repo_root), "files": files})
    try:
        proc = subprocess.run(
            ["node", str(_HELPER)],
            input=job, capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        edges = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return 0
    if not isinstance(edges, list):
        return 0

    added = 0
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        caller, caller_file = edge.get("caller"), edge.get("caller_file")
        callee, callee_file = edge.get("callee"), edge.get("callee_file")
        if not (caller and caller_file and callee and callee_file):
            continue
        predicate = _PREDICATE.get(edge.get("kind", "direct"), "calls")
        cid = kg.add_entity(caller, "SYMBOL", qualifier=caller_file)
        did = kg.add_entity(callee, "SYMBOL", qualifier=callee_file)
        if cid != did:
            kg.add_relation(cid, did, predicate, source=caller_file)
            added += 1
    kg.commit()
    return added
