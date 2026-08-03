"""The affordance engine — secagent's context-reduction core.

It turns a repository into compact, content-addressed artifacts so that a small or
local model can work from a minimal, budget-bounded context instead of raw source:

* ``project_map`` — pruned structure, components, entrypoints, language mix.
* ``symbols`` — a symbol index (functions/classes) via AST + heuristics.
* ``file_summary`` — per-file purpose + key symbols (heuristic, optionally LLM).
* ``io_map`` — cross-component IO edges (imports, HTTP, env, datastores).
* ``store`` — a SQLite + JSON store; indexing is incremental by content hash.
* ``retrieval`` — selects the smallest affordance set that fits a token budget.
"""

from .models import (
    Component,
    FileRecord,
    FileSummary,
    IOEdge,
    ProjectMap,
    Symbol,
)
from .store import AffordanceStore

__all__ = [
    "Component",
    "FileRecord",
    "FileSummary",
    "IOEdge",
    "ProjectMap",
    "Symbol",
    "AffordanceStore",
]
