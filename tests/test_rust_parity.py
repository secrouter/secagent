"""Rust-analyzer report parity: trait-interface resolution and intra-file call edges.

## The defect

`tools/secagent-rust-analyzer/src/main.rs`'s SCIP symbol parser (`parse()`, ~line 130)
qualifies an implemented trait with the *impl site's* namespace, not the trait's own
defining module — rust-analyzer's SCIP descriptor for a trait impl carries only the
trait's bare name, and the parser prefixes it with whatever `ns` happens to be at the
`impl` block. One real trait, implemented by types in N different modules, therefore
surfaces in the raw report as N different (wrong) interface strings, one per
implementing module.

Ground truth, from running the shipped `secagent-analyzer-rust:latest` image against the
real aho-corasick crate (https://github.com/BurntSushi/aho-corasick), saved at
`~/secagent/.rustreport/aho-corasick-report.json`: there is exactly one
`Automaton` trait, `automaton::Automaton` (kind `trait`, `src/automaton.rs:198`), but
`dfa::DFA`, `nfa::contiguous::NFA`, and `nfa::noncontiguous::NFA` each report a
DIFFERENT, wrong-namespace `Automaton` interface (`dfa::Automaton`,
`nfa::contiguous::Automaton`, `nfa::noncontiguous::Automaton` respectively) — none of
which exists anywhere in the report as its own trait definition.

## The fix

`src/secagent/affordances/analysis.py::_resolve_trait_interfaces` runs at ingest time (not
in the Rust analyzer, which cannot be rebuilt/verified here): it indexes every
`kind == "trait"` type record by short name, and for each `interfaces` entry that is not
itself a known trait definition, rewrites it to the matching definition IF the short
name resolves to exactly one. An ambiguous short name (two distinct real traits sharing
a name) or a name matching no known definition (e.g. an external-crate trait
rust-analyzer did not index) is left untouched rather than guessed.

## The fixture

`tests/fixtures/rust_aho_corasick/trait-fixture.json` is 8 of the real report's 103
`types[]` records, extracted verbatim (see the fixture's own README for exactly how).
It carries: the three automata and their broken interfaces, the one real
`automaton::Automaton` trait definition, the one already-correct real pair
(`util::prefilter::Packed` -> `util::prefilter::PrefilterI`), and both real `Pointer`
trait definitions (`util::int::Pointer`, `packed::ext::Pointer`) — the one genuinely
ambiguous short name in the whole aho-corasick report. It reproduces the FULL set of
`interfaces` entries in the real 345KB report (all 4 of them), so counting
rewritten/unchanged against this fixture is equivalent to counting against the real
report, not an approximation of it.

One thing the fixture does NOT reproduce, because the real crate doesn't either: no type
in the real report references `Pointer` as an interface at all, at any namespace. So the
over-merge (ambiguous short name) attack below adds one synthetic struct on top of the
fixture's two real `Pointer` definitions — documented inline as synthetic, and why.
"""

from __future__ import annotations

import json
from pathlib import Path

from secagent.affordances import analysis
from secagent.affordances.store import AffordanceStore

FIXTURE = Path(__file__).parent / "fixtures" / "rust_aho_corasick" / "trait-fixture.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _ingest(data: dict, tmp_path) -> dict[str, analysis.AnalysisType]:
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        analysis.ingest_report(analysis.parse_report(data), store)
        return {t.qualified_name: t for t in store.load_types()}
    finally:
        store.close()


# --- The defect, on real data -------------------------------------------------------


def test_three_automata_resolve_to_the_one_real_trait(tmp_path):
    """dfa::DFA, nfa::contiguous::NFA, nfa::noncontiguous::NFA each carried a DIFFERENT
    wrong-namespace `Automaton` interface in the raw report. All three must resolve to
    the single real `automaton::Automaton` trait definition."""
    types = _ingest(_load_fixture(), tmp_path)
    assert types["dfa::DFA"].interfaces == ["automaton::Automaton"]
    assert types["nfa::contiguous::NFA"].interfaces == ["automaton::Automaton"]
    assert types["nfa::noncontiguous::NFA"].interfaces == ["automaton::Automaton"]
    # The core assertion the whole task hangs on: "how do these relate" now has ONE
    # answer, not three.
    distinct = {types["dfa::DFA"].interfaces[0],
                types["nfa::contiguous::NFA"].interfaces[0],
                types["nfa::noncontiguous::NFA"].interfaces[0]}
    assert distinct == {"automaton::Automaton"}


def test_exactly_three_rewritten_one_untouched_on_real_data(tmp_path):
    """Quantitative pin on the real fixture: of the 4 interface references in the whole
    real report, exactly 3 (the wrong-namespace Automaton ones) must change and exactly
    1 (already-correct Packed -> PrefilterI) must not."""
    raw = _load_fixture()
    before = {t["qualified_name"]: list(t.get("interfaces", [])) for t in raw["types"]}
    types = _ingest(raw, tmp_path)

    rewritten = 0
    unchanged = 0
    for qname, before_ifaces in before.items():
        if not before_ifaces:
            continue
        after_ifaces = types[qname].interfaces
        if after_ifaces == before_ifaces:
            unchanged += 1
        else:
            rewritten += 1
    assert rewritten == 3, f"expected 3 rewritten interface refs, got {rewritten}"
    assert unchanged == 1, f"expected 1 untouched interface ref, got {unchanged}"


# --- Silence: already-correct names must not be touched -----------------------------


def test_already_correct_interface_is_left_alone(tmp_path):
    """`util::prefilter::Packed` -> `util::prefilter::PrefilterI` is ALREADY the real,
    correctly-qualified trait name in the raw report (verified against the full report:
    `PrefilterI` has no other type sharing its short name). It must survive unchanged —
    a resolver that rewrites something already right is as wrong as one that misses the
    real defect."""
    types = _ingest(_load_fixture(), tmp_path)
    assert types["util::prefilter::Packed"].interfaces == ["util::prefilter::PrefilterI"]


def test_all_correct_report_passes_through_unchanged(tmp_path):
    """A minimal report where every interface name is already a real, correctly
    qualified trait definition must come through byte-for-byte unchanged."""
    data = {
        "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
        "functions": [],
        "types": [
            {"qualified_name": "svc::Server", "kind": "struct", "file": "src/server.rs",
             "line": 10, "interfaces": ["svc::Handler"]},
            {"qualified_name": "svc::Handler", "kind": "trait", "file": "src/server.rs",
             "line": 5, "interfaces": []},
        ],
        "calls": [], "build": {},
    }
    types = _ingest(data, tmp_path)
    assert types["svc::Server"].interfaces == ["svc::Handler"]


# --- The attack that matters most: ambiguous short names must NOT merge -------------


def test_ambiguous_pointer_short_name_is_never_merged(tmp_path):
    """Real data: `util::int::Pointer` and `packed::ext::Pointer` are two DISTINCT real
    traits in the aho-corasick report that happen to share the short name `Pointer` —
    the one genuine short-name collision in the whole crate (verified by scanning all 19
    trait records). A resolver keyed on short names alone has a real chance of merging
    them into whichever sorts first, which would make two unrelated types look related
    with no signal it happened.

    No type in the real report actually references `Pointer` as an interface at any
    namespace, so the triggering REFERENCE below is synthetic — a struct whose
    `interfaces` names a third, wrong-namespace `Pointer` that matches neither real
    definition. Only that one type is synthetic; both `Pointer` trait definitions it is
    tested against are the real ones from the fixture.
    """
    data = _load_fixture()
    # synthetic reference: matches neither real Pointer definition's qualified name.
    synthetic_iface = "packed::rle::Pointer"
    data = {**data, "types": [*data["types"], {
        "qualified_name": "packed::rle::Block", "kind": "struct", "file": "src/packed/rle.rs",
        "line": 1, "interfaces": [synthetic_iface],
    }]}
    types = _ingest(data, tmp_path)
    # Left exactly as recorded — not merged to either real Pointer definition, and not
    # merged to whichever of the two happens to sort first.
    assert types["packed::rle::Block"].interfaces == [synthetic_iface]
    assert types["packed::rle::Block"].interfaces != ["util::int::Pointer"]
    assert types["packed::rle::Block"].interfaces != ["packed::ext::Pointer"]
    # And the two real definitions themselves are untouched and still distinct.
    assert types["util::int::Pointer"].kind == "trait"
    assert types["packed::ext::Pointer"].kind == "trait"
    real_a = types["util::int::Pointer"].qualified_name
    real_b = types["packed::ext::Pointer"].qualified_name
    assert real_a != real_b


# --- Attacking the mechanism ---------------------------------------------------------


def test_short_name_matching_a_struct_not_a_trait_is_not_resolved(tmp_path):
    """A struct sharing a trait's short name must not create a spurious candidate: only
    `kind == "trait"` records may resolve an interface reference."""
    data = {
        "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
        "functions": [],
        "types": [
            # A STRUCT (not trait) named "mod_a::Widget" -- must never be treated as a
            # trait definition candidate.
            {"qualified_name": "mod_a::Widget", "kind": "struct", "file": "a.rs", "line": 1,
             "interfaces": []},
            # Some other type wrongly references "mod_b::Widget" as an interface: since
            # there is NO trait named Widget anywhere, this must be left untouched, not
            # matched against the struct.
            {"qualified_name": "mod_c::Thing", "kind": "struct", "file": "c.rs", "line": 1,
             "interfaces": ["mod_b::Widget"]},
        ],
        "calls": [], "build": {},
    }
    types = _ingest(data, tmp_path)
    assert types["mod_c::Thing"].interfaces == ["mod_b::Widget"]


def test_no_trait_definitions_at_all_leaves_interfaces_untouched(tmp_path):
    """A report with zero trait records anywhere (e.g. only structs/enums were indexed)
    must not crash and must leave every interface reference exactly as recorded."""
    data = {
        "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
        "functions": [],
        "types": [
            {"qualified_name": "x::Foo", "kind": "struct", "file": "x.rs", "line": 1,
             "interfaces": ["external::Serialize"]},
        ],
        "calls": [], "build": {},
    }
    types = _ingest(data, tmp_path)
    assert types["x::Foo"].interfaces == ["external::Serialize"]


def test_empty_interfaces_list_is_not_touched(tmp_path):
    """A type with no interfaces at all must survive as an empty list, not None or a
    crash."""
    data = {
        "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
        "functions": [],
        "types": [
            {"qualified_name": "x::Foo", "kind": "struct", "file": "x.rs", "line": 1,
             "interfaces": []},
        ],
        "calls": [], "build": {},
    }
    types = _ingest(data, tmp_path)
    assert types["x::Foo"].interfaces == []


def test_external_crate_trait_not_indexed_is_left_as_is(tmp_path):
    """A trait from an external crate that rust-analyzer did not index has no matching
    definition anywhere in the report; it must be passed through, not dropped and not
    guessed at."""
    data = {
        "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
        "functions": [],
        "types": [
            {"qualified_name": "mycrate::Thing", "kind": "struct", "file": "t.rs", "line": 1,
             "interfaces": ["std::fmt::Debug"]},  # never indexed: std is external
        ],
        "calls": [], "build": {},
    }
    types = _ingest(data, tmp_path)
    assert types["mycrate::Thing"].interfaces == ["std::fmt::Debug"]


# --- 2.2 (second half): intra-file call edges for Rust -------------------------------


def test_rust_intra_file_call_edge_survives_resolution():
    """FIX_PLAN flagged intra-file call edges as absent for Rust. `_index_rust` in
    `affordances/api.py` already calls `resolve_calls(..., include_intra_file=True)`,
    the same as the clang and C# passes (see `_index_rust`, ~line 506). This pins that
    invariant directly against `resolve_calls`, in the spirit of the other call-map
    invariant tests (`tests/test_deep_analysis_merge.py`), rather than re-deriving it."""
    from secagent.affordances.call_map import build_definition_table, resolve_calls
    from secagent.affordances.clang_ast import CallSite, FuncDef

    funcs = [
        FuncDef(name="process", signature="fn process()", file="main.rs", line=2),
        FuncDef(name="helper", signature="fn helper()", file="main.rs", line=6),
    ]
    calls = [CallSite(caller="process", callee="helper", file="main.rs", line=3)]

    edges = resolve_calls(calls, build_definition_table(funcs), include_intra_file=True)
    same_file = [e for e in edges if e.src_file == e.dst_file == "main.rs"]
    assert same_file, "same-file Rust call edge was dropped"
    assert same_file[0].caller == "process" and same_file[0].callee == "helper"


# --- the correction is observable, not just documented ------------------------------

def _ingest_result(data: dict, tmp_path) -> dict:
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        return analysis.ingest_report(analysis.parse_report(data), store)
    finally:
        store.close()


def test_ingest_reports_how_many_interfaces_it_had_to_correct(tmp_path):
    """The rewrite is a workaround for a bug in a Rust binary this repo ships but cannot
    rebuild here, and both sides carry comments saying so. Comments are documentation,
    not a signal. `interfaces_resolved` is the signal: it counts what this ingest had to
    correct, so if the analyzer is ever fixed the number drops to 0 on a report that
    previously needed rewriting — and whoever fixed it finds out without reading either
    comment."""
    result = _ingest_result(_load_fixture(), tmp_path)
    assert result["interfaces_resolved"] == 3, (
        "the three wrong-namespace Automaton references are exactly what needed "
        "correcting in the real report"
    )


def test_a_report_needing_no_correction_reports_zero(tmp_path):
    """The silence half, and the shape a fixed analyzer would produce: every interface
    already naming a real trait definition means nothing to resolve, and the count says
    so rather than staying quietly non-zero."""
    raw = _load_fixture()
    for t in raw["types"]:
        if t.get("interfaces"):
            t["interfaces"] = ["automaton::Automaton"]      # already correct
    result = _ingest_result(raw, tmp_path)
    assert result["interfaces_resolved"] == 0
