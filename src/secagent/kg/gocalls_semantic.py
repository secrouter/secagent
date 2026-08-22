"""Semantic (type-resolved) Go call extraction — the "heavy" counterpart to
``treesitter_calls`` for Go, mirroring ``pyjedi`` for Python and ``tscalls_semantic``
for TS/JS.

Python has no Go type checker, so this shells out to a small Go helper
(``tools/kg-gocallgraph``) that drives the real toolchain: ``golang.org/x/tools/go/packages``
loads and type-checks the target module, ``go/ssa`` builds its SSA form, and
``go/callgraph`` (CHA — Class Hierarchy Analysis over every reachable method; RTA —
Rapid Type Analysis, tighter, used instead when the module has a ``main`` package to seed
it from) resolves each call — INCLUDING a call through an interface value — to its real
target(s). A call whose target lives outside the module directory (stdlib, a
third-party dependency) is dropped by the helper itself, the same discipline as jedi's
``module_path`` check for Python: its declaring file simply isn't under the module root.

RTA's reachability walk is seeded only from each ``main`` package's ``init``/``main``, so
on its own it would silently produce zero edges for any function unreachable from main —
an exported library API in a repo that is both a library and a cmd, or an unused helper.
The helper guards against that: it always computes CHA too and unions in CHA's edges for
any caller RTA's graph never reached at all, so deep-mode Go edges cover the whole
repo (RTA's tighter kind where it has an opinion, CHA's whole-repo recall everywhere
else) rather than being reachable-from-main only.

Requires the ``go`` toolchain on PATH; the helper is built once (``go build``, from
source vendored at ``tools/kg-gocallgraph``) and the resulting binary is cached on disk
and reused until that source changes. Guard a call with :func:`go_semantic_available`
first; degrades to 0 edges / unavailable on any missing toolchain, build failure, missing
``go.mod``, or helper error — never raises.
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

# tools/kg-gocallgraph/, resolved relative to this file (…/src/secagent/kg/
# gocalls_semantic.py -> repo root is four levels up), not the target repo being
# analyzed. Absent (e.g. a wheel install that only ships ``src/secagent``) => unavailable.
_TOOL_DIR = Path(__file__).resolve().parents[3] / "tools" / "kg-gocallgraph"
_SRC = _TOOL_DIR / "main.go"
_BINARY = _TOOL_DIR / "kg-gocallgraph"  # gitignored build artifact; built lazily, cached

# CHA/RTA reconstruct the whole module's type-checked SSA form; large repos need real time.
_BUILD_TIMEOUT_S = 180
_RUN_TIMEOUT_S = 300

# The helper's edge "kind" -> the graph predicate (mirrors project.py's _CALL_PREDICATE:
# a "direct" call is unconditional; "interface"/"virtual" dispatch is a possibly-one-
# of-several polymorphic edge, so it becomes the honest ``may_call``). An unrecognized
# kind (a future helper binary emitting something this mapping doesn't know) falls back
# to "may_call" too, not "calls" — an unknown dispatch shape is more safely treated as
# potentially polymorphic than claimed unconditional; keep this fallback and
# project.py's _CALL_PREDICATE fallback in lockstep.
_PREDICATE = {"direct": "calls", "interface": "may_call", "virtual": "may_call"}


@lru_cache(maxsize=1)
def go_semantic_available() -> bool:
    """True if ``go`` is on PATH and works, and the helper's source is present — i.e.
    the semantic Go extractor can in principle run (per-repo checks, e.g. a ``go.mod``
    at the target root, happen in :func:`extract_go_semantic` since this predicate takes
    no arguments, by the extractor registry's contract)."""
    if shutil.which("go") is None or not _SRC.is_file():
        return False
    try:
        proc = subprocess.run(
            ["go", "version"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _stale(binary: Path) -> bool:
    """True if ``binary`` doesn't exist or any helper source file is newer than it."""
    if not binary.is_file():
        return True
    try:
        bin_mtime = binary.stat().st_mtime
        return any(
            f.is_file() and f.stat().st_mtime > bin_mtime
            for f in (_SRC, _TOOL_DIR / "go.mod", _TOOL_DIR / "go.sum")
        )
    except OSError:
        return True


def _binary() -> Path | None:
    """Build (once; cached on disk at ``_BINARY`` and reused until the helper's own
    source changes) the ``kg-gocallgraph`` binary. Returns None on any build failure."""
    if not go_semantic_available():
        return None
    if not _stale(_BINARY):
        return _BINARY
    try:
        proc = subprocess.run(
            ["go", "build", "-o", str(_BINARY), "."],
            cwd=str(_TOOL_DIR), capture_output=True, timeout=_BUILD_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not _BINARY.is_file():
        return None
    return _BINARY


def extract_go_semantic(kg: KnowledgeGraph, store: AffordanceStore) -> int:
    """Add type-resolved Go ``calls``/``may_call`` edges to ``kg``. Returns the edge
    count (0 if the helper is unavailable, the repo has no ``go.mod``, or it fails —
    never raises)."""
    repo_root = Path(store.repo_root)
    if not (repo_root / "go.mod").is_file():
        return 0  # not a Go module the helper's `go/packages` load can resolve
    binary = _binary()
    if binary is None:
        return 0
    try:
        proc = subprocess.run(
            [str(binary), "-dir", str(repo_root)],
            capture_output=True, timeout=_RUN_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0

    added = 0
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            edge = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(edge, dict):
            continue
        caller, caller_file = edge.get("caller_func"), edge.get("caller_file")
        callee, callee_file = edge.get("callee_func"), edge.get("callee_file")
        if not (caller and caller_file and callee and callee_file):
            continue
        predicate = _PREDICATE.get(edge.get("kind", "direct"), "may_call")
        cid = kg.add_entity(caller, "SYMBOL", qualifier=caller_file)
        did = kg.add_entity(callee, "SYMBOL", qualifier=callee_file)
        if cid != did:  # drop a symbol resolving to itself (recursion / namesake)
            kg.add_relation(cid, did, predicate, source=caller_file)
            added += 1
    kg.commit()
    return added
