"""The `secagent-analysis/v1` contract: a normalized report from a heavy (compiled)
analysis backend, plus its ingest into the affordance store.

Heavy backends (Roslyn/MSBuild for C#, a real clang build for C/C++) live in optional
container images (see ``docs/design/heavy-analysis-pipeline.md``). They all emit this
single JSON shape so ingest, the store, and the docs are written once and stay
backend-agnostic. The light syntactic backends remain the default; this is purely
additive (Phase 0: the contract + ingest, testable without any container).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .models import CallEdge, Symbol, TypeRecord
from .store import AffordanceStore

SCHEMA_VERSION = "secagent-analysis/v1"


@dataclass
class AnalysisFunction:
    name: str
    qualified_name: str
    signature: str
    file: str
    line: int
    kind: str = "function"  # function | method | constructor
    owning_type: str = ""


@dataclass
class AnalysisType:
    qualified_name: str
    kind: str  # class | struct | interface | record | enum
    file: str
    line: int = 0
    bases: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)


@dataclass
class AnalysisCall:
    caller_qualified: str
    callee_qualified: str
    callee_file: str = ""
    file: str = ""  # call-site file; if absent, derived from the caller's file
    line: int = 0
    edge_kind: str = "direct"  # direct | virtual | interface


@dataclass
class AnalysisReport:
    language: str
    backend: str
    functions: list[AnalysisFunction] = field(default_factory=list)
    types: list[AnalysisType] = field(default_factory=list)
    calls: list[AnalysisCall] = field(default_factory=list)
    build: dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA_VERSION


def _build(cls, d: dict[str, Any]):
    """Construct a dataclass from a dict, ignoring unknown keys (forward-compatible)."""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


def parse_report(data: dict[str, Any]) -> AnalysisReport:
    """Validate and parse a ``secagent-analysis/v1`` report."""
    if not isinstance(data, dict):
        raise ValueError("analysis report must be a JSON object")
    schema = data.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported analysis schema {schema!r}; expected {SCHEMA_VERSION!r}")
    if not data.get("language") or not data.get("backend"):
        raise ValueError("analysis report requires 'language' and 'backend'")
    return AnalysisReport(
        language=str(data["language"]),
        backend=str(data["backend"]),
        functions=[_build(AnalysisFunction, f) for f in data.get("functions", [])],
        types=[_build(AnalysisType, t) for t in data.get("types", [])],
        calls=[_build(AnalysisCall, c) for c in data.get("calls", [])],
        build=dict(data.get("build", {})),
        schema=schema,
    )


def _short_name(qualified: str) -> str:
    for sep in ("::", "."):
        if sep in qualified:
            qualified = qualified.rsplit(sep, 1)[-1]
    return qualified


def _resolve_trait_interfaces(types: list[AnalysisType]) -> tuple[list[AnalysisType], int]:
    """Correct trait-impl interface names mis-qualified by the impl site's namespace.

    The Rust analyzer's SCIP-symbol parser (``tools/secagent-rust-analyzer/src/main.rs``,
    ``parse()``, the ``itrait`` construction) prefixes an implemented trait's short name
    with the namespace of the IMPL BLOCK, not the trait's own defining module —
    rust-analyzer's SCIP descriptor for a trait impl carries only the trait's bare name.
    A single trait implemented by types in N different modules therefore surfaces in the
    raw report as N different (wrong) qualified names, one per impl site — measured on
    the real aho-corasick crate: one real ``automaton::Automaton`` trait became
    ``dfa::Automaton``, ``nfa::contiguous::Automaton``, and
    ``nfa::noncontiguous::Automaton``, three distinct strings for one relationship.

    This cannot be fixed in the analyzer without a container rebuild this project cannot
    verify, and it would not repair reports already produced — so it is fixed here, at
    ingest, using ground truth already present in the SAME report: every ``kind ==
    "trait"`` type record gives a trait's real qualified name.

    For each ``interfaces`` entry that is not itself a known trait definition, rewrite it
    to the matching definition IF its short name resolves to EXACTLY ONE trait
    definition. An ambiguous short name — two genuinely distinct traits in different
    modules sharing a name, e.g. aho-corasick's real ``util::int::Pointer`` and
    ``packed::ext::Pointer`` — is left untouched rather than merged to whichever sorts
    first: over-merging makes unrelated types look related with no signal it happened,
    which is worse than the original N-copies defect. A name matching no trait
    definition at all (e.g. an external-crate trait rust-analyzer did not index) is also
    left untouched, not dropped and not guessed.
    """
    trait_quals = {t.qualified_name for t in types if t.kind == "trait"}
    by_short: dict[str, list[str]] = {}
    for t in types:
        if t.kind == "trait":
            by_short.setdefault(_short_name(t.qualified_name), []).append(t.qualified_name)

    out: list[AnalysisType] = []
    resolved = 0
    for t in types:
        if not t.interfaces:
            out.append(t)
            continue
        new_ifaces = []
        changed = False
        for iface in t.interfaces:
            if iface in trait_quals:
                new_ifaces.append(iface)  # already a real definition: leave alone
                continue
            candidates = by_short.get(_short_name(iface), [])
            if len(candidates) == 1:
                new_ifaces.append(candidates[0])
                changed = True
                resolved += 1
            else:
                new_ifaces.append(iface)  # no match, or ambiguous: leave as recorded
        out.append(dataclasses.replace(t, interfaces=new_ifaces) if changed else t)
    return out, resolved


def ingest_report(report: AnalysisReport, store: AffordanceStore) -> dict[str, int | bool]:
    """Apply a heavy-analysis report to the store: symbols, types, and the call map.

    Symbols and types are stored with their qualified names; call edges are already
    resolved (qualified caller/callee + callee file), so they need no name-matching.
    """
    qual_to_file = {fn.qualified_name: fn.file for fn in report.functions if fn.qualified_name}

    # Resolve trait-impl interface names before anything else consumes them: a backend
    # (currently only the Rust analyzer) may report a trait's qualified name wrong,
    # namespaced to the impl site rather than the trait's own module. See
    # `_resolve_trait_interfaces`.
    types, interfaces_resolved = _resolve_trait_interfaces(report.types)

    # Functions + types become symbols (grouped per file; set_symbols replaces a file's).
    by_file: dict[str, list[Symbol]] = {}
    for fn in report.functions:
        kind = "method" if fn.kind in ("method", "constructor") else "function"
        by_file.setdefault(fn.file, []).append(
            Symbol(fn.name, kind, fn.file, fn.line, fn.signature, fn.owning_type, "",
                   fn.qualified_name))
    for t in types:
        by_file.setdefault(t.file, []).append(
            Symbol(_short_name(t.qualified_name), "class", t.file, t.line, "", "", "",
                   t.qualified_name))
    for file, syms in by_file.items():
        store.set_symbols(file, syms)

    store.set_types([
        TypeRecord(t.qualified_name, t.kind, t.file, t.line, list(t.bases), list(t.interfaces))
        for t in types
    ])

    # Start from what is already indexed and MERGE, letting the heavy backend win on
    # conflicts. Replacing outright made `analyze deep` — advertised as the accurate
    # upgrade — silently DELETE every edge the light pass had found but the compiled
    # backend did not re-derive. Measured on a Rust crate: 584 inter-file + 645
    # intra-file edges before, 329 total and zero intra-file after. The CLI reported it
    # as a success, with real types and "resolved" edges, while the call graph regressed.
    edges: dict[tuple, CallEdge] = {e.key(): e for e in store.load_call_edges()}
    deep = 0
    for c in report.calls:
        src = c.file or qual_to_file.get(c.caller_qualified, "")
        dst = c.callee_file or qual_to_file.get(c.callee_qualified, "")
        # Intra-file edges are kept deliberately. "Who calls X" is a question about a
        # symbol, not about files, and the light path stores them for exactly that
        # reason; dropping them here reverted that fix for anyone who ran the heavy pass.
        if not src or not dst:
            continue
        edge = CallEdge(src, dst, c.caller_qualified, c.callee_qualified, c.edge_kind,
                        c.line)
        edges[edge.key()] = edge
        deep += 1
    store.set_call_map(list(edges.values()))

    store.commit()
    result: dict[str, int | bool] = {
        "functions": len(report.functions),
        "types": len(report.types),
        "call_edges": len(edges),
        "call_edges_from_backend": deep,
        # How many interface names this ingest had to correct. It is a signal, not
        # trivia: the correction exists only because the Rust analyzer namespaces an
        # implemented trait to its impl site (see `_resolve_trait_interfaces`). If that
        # binary is ever fixed, this drops to 0 on a report that previously needed
        # rewriting — which is how anyone finds out, without reading either comment.
        "interfaces_resolved": interfaces_resolved,
    }
    # Surface a degraded build in the machine-readable result, not just the log line
    # `heavy.py` already emits: `cli.py`'s `analyze deep` prints exactly this dict, and
    # that JSON is what a user (or the pi extension, which forwards stdout verbatim into
    # a model's context) actually reads. Without this, a run against an unrestored
    # project is byte-identical in its JSON to a healthy one, even though the call graph
    # it produced was worse — the exact defect class this project keeps finding.
    #
    # Omitted, not defaulted to True, when the backend didn't report it at all (e.g. the
    # Rust backend may not populate `build.restored`) — same house pattern as `callers`'
    # `line` key in `queries.py`: absence is honest, a guessed default is not.
    if "restored" in report.build:
        result["restored"] = bool(report.build["restored"])
    return result
