"""Deterministic projector: the affordance graph -> the knowledge graph.

secagent's advantage over the artifact's LLM-extraction step: for code, the facts are
already extracted, exactly and reproducibly, by the analyzers into the affordance store
(symbols, calls, types, io). This projects those into the general
``kg_entities``/``kg_relations``/``kg_aliases`` shapes with **no LLM at all** — so the
graph is a faithful, re-runnable mirror of the code, and a cheap model can answer over
it. Other domains (ops docs, agent memory) plug in LLM extractors writing the same
shapes; this is just the first, deterministic extractor.

Ontology for code (small and closed, per the design):
  entity types  — SYMBOL (any code identifier), FILE, COMPONENT, EXTERNAL
  relationships — defined_in, calls, inherits, implements, and the io predicates below
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .store import KnowledgeGraph

if TYPE_CHECKING:  # avoid importing the heavy affordances package at module load
    from ..affordances.models import Symbol
    from ..affordances.store import AffordanceStore

# IOEdge.kind -> a graph predicate. Names read as "src <predicate> dst".
_IO_PREDICATE = {
    "import": "imports",
    "http_endpoint": "exposes",
    "http_call": "calls_http",
    "env": "reads_env",
    "datastore": "uses_datastore",
    "messaging": "uses_messaging",
    "socket": "uses_socket",
    "file_io": "file_io",
    "cli": "exposes_cli",
}


def _symbol_description(sym: Symbol) -> str:
    """A compact note carrying the symbol's kind, signature, and one-line doc — the
    conditions/detail the design says belong in the entity description, not the edges."""
    parts = [sym.kind]
    if sym.signature:
        parts.append(sym.signature)
    if sym.doc:
        parts.append(sym.doc)
    return " — ".join(parts)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def project_affordances(kg: KnowledgeGraph, store: AffordanceStore) -> dict[str, int]:
    """Project an indexed affordance ``store`` into ``kg`` and return the new counts.

    Idempotent: entity identity is computed, so a re-project of unchanged code writes the
    same rows. The caller is expected to :meth:`KnowledgeGraph.clear` first for a full
    rebuild (so facts for deleted code do not linger); this function only adds.
    """
    file_paths: set[str] = set()

    # Files + the symbols defined in them.
    for rec in store.file_records():
        file_paths.add(rec.path)
        fid = kg.add_entity(rec.path, "FILE", source=rec.path)
        base = _basename(rec.path)
        if base != rec.path:
            kg.add_alias(fid, base)  # seed on "store.py" -> "src/secagent/kg/store.py"
        for sym in store.symbols_for_file(rec.path):
            canonical = sym.qualified_name or sym.name
            # Qualify the symbol by its defining file, so same-named symbols in different
            # files are distinct nodes (not one conflated node with cross-file edges).
            sid = kg.add_entity(
                canonical, "SYMBOL", qualifier=sym.file,
                description=_symbol_description(sym), source=sym.file,
            )
            # Keep the bare name reachable when the canonical form is qualified.
            if sym.qualified_name and sym.name and sym.name != sym.qualified_name:
                kg.add_alias(sid, sym.name)
            kg.add_relation(sid, fid, "defined_in", source=sym.file)

    # Call edges from the affordance call map: it already resolved each endpoint to its
    # defining file (src_file for the caller, dst_file for the callee), so qualify by those.
    for edge in store.load_call_edges():
        if not (edge.caller and edge.callee):
            continue
        cid = kg.add_entity(edge.caller, "SYMBOL", qualifier=edge.src_file, source=edge.src_file)
        did = kg.add_entity(edge.callee, "SYMBOL", qualifier=edge.dst_file, source=edge.dst_file)
        if cid != did:  # drop a symbol resolving to itself (recursion / namesake)
            kg.add_relation(cid, did, "calls", source=edge.src_file)

    # Types: inheritance / interface implementation. Qualify by file, and resolve a base to
    # its own defining file when the repo declares it (else leave it unqualified — an
    # external base).
    types = store.load_types()
    type_files = {t.qualified_name: t.file for t in types}
    for typ in types:
        tid = kg.add_entity(
            typ.qualified_name, "SYMBOL", qualifier=typ.file, description=typ.kind, source=typ.file
        )
        for base in typ.bases:
            bid = kg.add_entity(base, "SYMBOL", qualifier=type_files.get(base, ""))
            kg.add_relation(tid, bid, "inherits", source=typ.file)
        for iface in typ.interfaces:
            iid = kg.add_entity(iface, "SYMBOL", qualifier=type_files.get(iface, ""))
            kg.add_relation(tid, iid, "implements", source=typ.file)

    # IO map: imports, endpoints, datastores, env, messaging, sockets, cli.
    for io in store.load_io_edges():
        sid = kg.add_entity(*_io_endpoint(io.src, file_paths), source=io.src)
        did = kg.add_entity(*_io_endpoint(io.dst, file_paths), source=io.detail or io.dst)
        predicate = _IO_PREDICATE.get(io.kind, io.kind)
        kg.add_relation(sid, did, predicate, source=io.detail)

    kg.commit()
    return kg.counts()


def _io_endpoint(name: str, file_paths: set[str]) -> tuple[str, str]:
    """Resolve an IO endpoint to ``(name, entity_type)``.

    An endpoint that names a known repo file becomes the FILE entity (so ``imports``
    edges connect to the symbols defined in that file); anything else is an EXTERNAL
    (a URL, host, datastore, env var, queue) the code touches.
    """
    return (name, "FILE") if name in file_paths else (name, "EXTERNAL")


def build(repo, settings, *, store_dir: str | None = None) -> dict[str, int]:
    """Index the repo if needed, then (re)build its knowledge graph. Returns KG counts.

    ``store_dir`` defaults to the affordance store's, so the KG lives in the same
    ``index.db`` as the affordances it is projected from.
    """
    from ..affordances import queries
    from .pycalls import extract_python_calls
    from .treesitter_calls import extract_treesitter_calls

    sd = store_dir or settings.affordances.store_dir
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=sd) as kg:
            kg.clear()
            project_affordances(kg, store)
            # The affordance light path extracts no call edges for Python/Go/TS; add them
            # directly (ast for Python, tree-sitter for Go/TS/JS) so call-chain recall works.
            extract_python_calls(kg, store)
            extract_treesitter_calls(kg, store)
            return kg.counts()
    finally:
        store.close()
