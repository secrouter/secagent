"""The heavy analysis pass must not destroy what the light pass found.

`secagent analyze deep` runs a compiled backend (Roslyn for C#, rust-analyzer for Rust) and
is advertised as the accurate upgrade. It ingested its results by REPLACING the call map
outright and by discarding every intra-file edge, so running it silently deleted edges
the light index had correctly found. Measured on a Rust crate by an evaluation agent:
584 inter-file + 645 intra-file edges before, 329 total and **zero** intra-file after —
while the command reported success, with real types and "resolved" edges.

Dropping intra-file edges also reverted the same-file `callers` fix: "who calls X" is a
question about a symbol, not about files, which is why the light path stores them.
"""

from __future__ import annotations

from secagent.affordances.analysis import ingest_report, parse_report
from secagent.affordances.models import CallEdge
from secagent.affordances.store import AffordanceStore


def _report(calls, types=(), functions=()):
    """Build the minimal `secagent-analysis/v1` shape ingest_report consumes."""
    return parse_report({
        "schema": "secagent-analysis/v1",
        "language": "Rust",
        "backend": "rust-analyzer",
        "functions": list(functions),
        "types": list(types),
        "calls": list(calls),
    })


def _edge(src, dst, caller, callee):
    return {"file": src, "callee_file": dst, "caller_qualified": caller,
            "callee_qualified": callee, "edge_kind": "direct"}


def test_existing_edges_survive_the_heavy_pass(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([
            CallEdge("a.rs", "b.rs", "a::one", "b::two", "direct"),
            CallEdge("c.rs", "d.rs", "c::three", "d::four", "direct"),
        ])
        store.commit()

        ingest_report(_report([_edge("x.rs", "y.rs", "x::five", "y::six")]), store)

        keys = {(e.src_file, e.dst_file) for e in store.load_call_edges()}
        assert ("a.rs", "b.rs") in keys, "the heavy pass deleted a light-index edge"
        assert ("c.rs", "d.rs") in keys
        assert ("x.rs", "y.rs") in keys, "the heavy pass's own edge is missing"
    finally:
        store.close()


def test_intra_file_edges_are_kept(tmp_path):
    """`callers` needs them: a function called from a switch a few hundred lines down in
    the same file is still a caller."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        ingest_report(_report([_edge("m.rs", "m.rs", "m::caller", "m::callee")]), store)
        edges = store.load_call_edges()
        assert any(e.src_file == e.dst_file == "m.rs" for e in edges), \
            "same-file call edge was discarded"
    finally:
        store.close()


def test_intra_file_edges_already_indexed_are_not_wiped(tmp_path):
    """The measured regression: intra-file count went to zero after the heavy pass."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("m.rs", "m.rs", "m::a", "m::b", "direct")])
        store.commit()
        ingest_report(_report([_edge("p.rs", "q.rs", "p::x", "q::y")]), store)
        assert any(e.src_file == e.dst_file == "m.rs" for e in store.load_call_edges())
    finally:
        store.close()


def test_the_backend_wins_on_a_conflicting_edge(tmp_path):
    """Merging must not mean "keep the stale one" — the compiled backend is the more
    accurate source, so its version of the same edge takes precedence."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.rs", "b.rs", "a::one", "b::two", "heuristic")])
        store.commit()
        ingest_report(_report([{
            "file": "a.rs", "callee_file": "b.rs", "caller_qualified": "a::one",
            "callee_qualified": "b::two", "edge_kind": "direct"}]), store)
        edge = next(e for e in store.load_call_edges()
                    if (e.src_file, e.dst_file) == ("a.rs", "b.rs"))
        assert edge.edge_kind == "direct"
    finally:
        store.close()


def test_report_distinguishes_backend_edges_from_the_total(tmp_path):
    """A merged total that looks like the backend's own output would hide a backend that
    resolved almost nothing."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.rs", "b.rs", "a::one", "b::two", "direct")])
        store.commit()
        out = ingest_report(_report([_edge("x.rs", "y.rs", "x::f", "y::g")]), store)
        assert out["call_edges_from_backend"] == 1
        assert out["call_edges"] == 2
    finally:
        store.close()


# --- The additive-merge invariant, as a property -----------------------------------
#
# The tests above each pin one measured shape of the Rust regression. The property that
# actually protects against the NEXT backend is more general: a heavy ingest must never
# reduce the stored call-edge count, full stop — regardless of how few edges (including
# zero, the unrestored/failed-run case) the backend reports. Issue #91 found this same
# class of bug independently in the Rust path, so it is not C#-specific even though a C#
# (unrestored Roslyn) run is where it was most recently found.
#
# This property already holds in the current code (`ingest_report` starts its merge from
# `store.load_call_edges()`, not from `{}`), so these tests pass before AND after this
# change — there is no "before" fix to pin here. The fail-first evidence for a property
# test is a deliberate mutation: temporarily make `ingest_report` start from `{}` instead
# of the store's edges (i.e. revert to the old REPLACE semantics) and confirm these tests
# catch it. See the task report for that mutation's output.

def test_heavy_ingest_with_fewer_edges_never_reduces_the_stored_count(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([
            CallEdge("a.rs", "b.rs", "a::one", "b::two", "direct"),
            CallEdge("c.rs", "d.rs", "c::three", "d::four", "direct"),
            CallEdge("e.rs", "f.rs", "e::five", "f::six", "direct"),
        ])
        store.commit()
        before = len(store.load_call_edges())
        assert before == 3

        # The backend reports a single edge -- far fewer than what's already indexed.
        ingest_report(_report([_edge("a.rs", "b.rs", "a::one", "b::two")]), store)

        after = len(store.load_call_edges())
        assert after >= before, (
            "a heavy ingest reporting FEWER edges than the store reduced the stored "
            f"call-edge count ({before} -> {after})")
    finally:
        store.close()


def test_heavy_ingest_with_zero_edges_never_reduces_the_stored_count(tmp_path):
    """The degenerate case that actually broke: an unrestored/failed heavy run reports
    ZERO calls, and that must not overwrite a light index that found real edges."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([
            CallEdge("a.rs", "b.rs", "a::one", "b::two", "direct"),
            CallEdge("c.rs", "d.rs", "c::three", "d::four", "direct"),
        ])
        store.commit()
        before = len(store.load_call_edges())
        assert before == 2

        ingest_report(_report([]), store)  # zero calls from the backend

        after = len(store.load_call_edges())
        assert after == before, (
            "an unrestored (zero-edge) heavy run changed the stored call-edge count "
            f"({before} -> {after})")
    finally:
        store.close()


def test_heavy_ingest_into_an_empty_store(tmp_path):
    """Attack case: nothing indexed yet. The invariant is trivially satisfied (anything
    >= 0), but the ingest must still not error and must still store the backend's edges."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        before = len(store.load_call_edges())
        assert before == 0

        ingest_report(_report([_edge("x.rs", "y.rs", "x::a", "y::b")]), store)

        after = len(store.load_call_edges())
        assert after >= before
        assert after == 1
    finally:
        store.close()


def test_heavy_ingest_with_more_edges_keeps_them_all(tmp_path):
    """Attack case: the backend reports MORE edges than the store has. The invariant
    (never fewer) must not be satisfied by accident -- the extra edges must survive."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.rs", "b.rs", "a::one", "b::two", "direct")])
        store.commit()
        before = len(store.load_call_edges())
        assert before == 1

        ingest_report(_report([
            _edge("a.rs", "b.rs", "a::one", "b::two"),
            _edge("x.rs", "y.rs", "x::f", "y::g"),
            _edge("p.rs", "q.rs", "p::m", "q::n"),
        ]), store)

        after = len(store.load_call_edges())
        assert after >= before
        assert after == 3
    finally:
        store.close()
