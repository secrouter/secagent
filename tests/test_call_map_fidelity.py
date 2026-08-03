"""The call map must admit when it is incomplete.

From an agentic test run against NASA cFS: `callers FM_GetFileInfoCmd` returned "No
callers found" while `fm_dispatch.c` called it two lines from a dispatch switch. The
cause was a degraded clang parse (missing headers) silently dropping ~75% of the call
graph. An empty result that cannot be distinguished from "genuinely no callers" makes an
engineer conclude live code is dead — so an incomplete map must say so.
"""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.clang_ast import ParsedUnit
from secagent.affordances.models import CallEdge
from secagent.affordances.store import AffordanceStore

_HEALTH_DEGRADED = {
    "total": 8, "degraded": 6, "compile_db": False,
    "top_missing": [["cfe.h", 6], ["osapi.h", 4]], "sample": ["fsw/src/fm_cmds.c"],
}
_HEALTH_CLEAN = {"total": 8, "degraded": 0, "compile_db": True,
                 "top_missing": [], "sample": []}


def test_parsed_unit_degraded_tracks_missing_headers_not_error_count():
    """`errors > 0` is a noisy proxy — a file can carry an unrelated error and still
    produce perfect types. Missing headers are the signal that actually degrades output."""
    assert not ParsedUnit(parsed=True, errors=3).degraded
    assert ParsedUnit(parsed=True, errors=1, missing_headers=["cfe.h"]).degraded


def test_store_round_trips_parse_health(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        assert store.parse_health() == {}          # never recorded
        store.set_parse_health(_HEALTH_DEGRADED)
        assert store.parse_health()["degraded"] == 6
    finally:
        store.close()


def test_store_parse_health_tolerates_corruption(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store._set_meta("clang_parse_health", "not json")
        store.commit()
        assert store.parse_health() == {}          # degrades, never raises
    finally:
        store.close()


def test_callers_empty_warns_when_call_map_is_incomplete(tmp_path):
    """The headline fix: an empty result must not read as 'there are no callers'."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_parse_health(_HEALTH_DEGRADED)
        out = json.loads(queries.callers(store, "FM_GetFileInfoCmd"))
        note = out["note"]
        assert "INCOMPLETE" in note
        assert "does NOT prove there are no callers" in note
        assert "6 of 8" in note
        assert "cfe.h" in note                      # actionable: what to fix
        assert "clang_compile_db" in note           # actionable: how to fix it
    finally:
        store.close()


def test_callers_empty_stays_quiet_when_parse_was_clean(tmp_path):
    """No false alarms: a clean parse must not cry wolf about fidelity."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_parse_health(_HEALTH_CLEAN)
        note = json.loads(queries.callers(store, "Nope"))["note"]
        assert "INCOMPLETE" not in note and "WARNING" not in note
    finally:
        store.close()


def test_calls_flags_a_partial_map_even_when_it_returns_edges(tmp_path):
    """Under-reporting is the risk: a partial map looks exactly like a complete one."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.c", "b.c", "f", "g", "direct")])
        store.set_parse_health(_HEALTH_DEGRADED)
        out = queries.calls(store)
        assert "a.c -> b.c" in out                  # the data it does have
        assert "INCOMPLETE" in out                  # and an honest caveat
    finally:
        store.close()


def test_calls_clean_parse_has_no_caveat(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.c", "b.c", "f", "g", "direct")])
        store.set_parse_health(_HEALTH_CLEAN)
        out = queries.calls(store)
        assert "a.c -> b.c" in out and "INCOMPLETE" not in out
    finally:
        store.close()


# --- system vs project headers ------------------------------------------------
# Caught by iteration 2 of the agentic test rig, on the fix above: with a correct
# compile DB the cFS call map was COMPLETE (144 edges, correct signatures) yet every
# file still reported a missing header — `stdint.h`, because a pip-installed libclang
# ships no libc headers. Counting those made the warning fire on a healthy parse, and a
# warning that always fires is one nobody reads.

def test_missing_system_headers_do_not_mark_a_file_degraded():
    from secagent.affordances.clang_ast import is_system_header

    u = ParsedUnit(parsed=True, missing_headers=["stdint.h", "stdbool.h", "stdio.h"])
    assert not u.degraded
    assert u.missing_project_headers == []
    assert is_system_header("stdint.h")
    assert is_system_header("/usr/include/stdio.h")   # matched by basename
    assert is_system_header("vector")                 # extensionless C++ header


def test_missing_project_headers_do_mark_a_file_degraded():
    u = ParsedUnit(parsed=True,
                   missing_headers=["stdint.h", "config/default_fm_tblstruct.h"])
    assert u.degraded
    assert u.missing_project_headers == ["config/default_fm_tblstruct.h"]


def test_project_header_detection_is_not_fooled_by_a_similar_name():
    from secagent.affordances.clang_ast import is_system_header

    # A project header that merely lives next to a std-sounding name is still project.
    assert not is_system_header("fm_stdint.h")
    assert not is_system_header("src/inc/time_utils.h")


# --- intra-file callers -------------------------------------------------------
# Cohort 2 (devC): `callers FM_ChildDeleteCmd` said "No callers found" while
# FM_ChildProcess calls it from a switch ~200 lines up in the SAME file. "Who calls X"
# is a question about a symbol, not about files — but the call map only kept
# cross-file edges, so same-file callers were discarded at index time.

def test_resolve_calls_can_keep_intra_file_edges():
    from secagent.affordances.call_map import CallSite, inter_file, resolve_calls

    calls = [
        CallSite("FM_ChildProcess", "FM_ChildDeleteCmd", "fm_child.c", 228),  # same file
        CallSite("FM_ProcessGroundCommand", "FM_GetFileInfoCmd", "fm_dispatch.c", 160),
    ]
    defs = {"FM_ChildDeleteCmd": "fm_child.c", "FM_GetFileInfoCmd": "fm_cmds.c"}

    # Default keeps the historical file->file-only behaviour.
    assert len(resolve_calls(calls, defs)) == 1

    both = resolve_calls(calls, defs, include_intra_file=True)
    assert len(both) == 2
    # ...and the file->file view still sees only the crossing edge.
    assert [e.callee for e in inter_file(both)] == ["FM_GetFileInfoCmd"]


def test_callers_finds_a_same_file_caller(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([
            CallEdge("fm_child.c", "fm_child.c", "FM_ChildProcess", "FM_ChildDeleteCmd",
                     "direct", 84),
        ])
        out = json.loads(queries.callers(store, "FM_ChildDeleteCmd"))
        assert out[0]["caller"] == "FM_ChildProcess"
        assert out[0]["path"] == "fm_child.c"
        assert out[0]["line"] == 84
        # `dispatch`, not `kind`: `find_symbol`/`types` already use `kind` for symbol
        # kinds, and this vocabulary is direct/virtual/interface.
        assert out[0]["dispatch"] == "direct"
    finally:
        store.close()


def test_repeated_call_sites_stay_one_edge_at_the_lowest_line(tmp_path):
    """A caller that calls the same function three times is one answer to "who calls
    this", not three.

    This goes through `resolve_calls`, where the dedup actually lives, rather than
    handing pre-made edges to `set_call_map` — putting `line` into `CallEdge.key()`
    would multiply every edge by its call-site count and inflate both `calls` and
    `callers`, and a test that dedups in the query instead cannot see that mistake.
    The retained site is the LOWEST line, so the representative does not depend on the
    order the parser happened to walk the AST in.
    """
    from secagent.affordances.call_map import CallSite, resolve_calls

    edges = resolve_calls(
        [CallSite("Caller", "Callee", "a.c", 90),
         CallSite("Caller", "Callee", "a.c", 12),
         CallSite("Caller", "Callee", "a.c", 45)],
        {"Callee": "b.c"},
    )
    assert len(edges) == 1, f"one caller/callee pair is one edge: {edges}"
    assert edges[0].line == 12, "the first call site is the representative"

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map(edges)
        out = json.loads(queries.callers(store, "Callee"))
        assert len(out) == 1 and out[0]["line"] == 12
    finally:
        store.close()


def _old_calls_table(store: AffordanceStore) -> None:
    """Rebuild `calls` in the pre-migration shape — no `line` column — and put one edge
    in it, exactly as a store indexed before call sites were recorded would hold it."""
    store.db.executescript(
        "DROP TABLE IF EXISTS calls;"
        "CREATE TABLE calls ("
        "  src_file TEXT, dst_file TEXT, caller TEXT, callee TEXT,"
        "  edge_kind TEXT DEFAULT 'direct');"
    )
    store.db.execute(
        "INSERT INTO calls (src_file, dst_file, caller, callee, edge_kind) "
        "VALUES ('a.c', 'a.c', 'Caller', 'Callee', 'direct')"
    )
    store.db.commit()


def test_a_store_indexed_before_call_sites_existed_still_opens(tmp_path):
    """The migration follows the `edge_kind` precedent: one idempotent `add_col`, no
    schema version bump. An existing store must keep working without a reindex —
    reopening it is what applies the column."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _old_calls_table(store)
    finally:
        store.close()

    store = AffordanceStore(tmp_path, ".secagent")     # reopening runs _migrate
    try:
        cols = {r["name"] for r in store.db.execute("PRAGMA table_info(calls)")}
        assert "line" in cols, "reopening must add the column, not crash on it"
        edges = store.load_call_edges()
        assert len(edges) == 1 and edges[0].line == 0, "the legacy row survives, line 0"
    finally:
        store.close()


def test_callers_omits_the_line_key_for_a_legacy_edge(tmp_path):
    """0 means unknown, and unknown must be silence. Emitting `"line": 0` would make a
    stale store fabricate a call site on the first line of every file — a location that
    is confidently wrong, which sends a reader somewhere, where an absent key sends them
    to `secagent index`. The tool description says the key appears after a reindex."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _old_calls_table(store)
    finally:
        store.close()

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        out = json.loads(queries.callers(store, "Callee"))
        assert out[0]["path"] == "a.c" and out[0]["caller"] == "Caller"
        assert "line" not in out[0], f"a line nobody recorded must not be reported: {out}"
    finally:
        store.close()


def test_callers_reports_the_line_once_the_edge_has_one(tmp_path):
    """The silent counterpart: the omission above must be driven by the data, not by
    `callers` having quietly stopped emitting the key at all."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([CallEdge("a.c", "a.c", "Caller", "Callee", "direct", 7)])
        out = json.loads(queries.callers(store, "Callee"))
        assert out[0]["line"] == 7
    finally:
        store.close()


def test_calls_map_still_excludes_intra_file_edges(tmp_path):
    """The file->file map must not gain self-edges from this change."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map([
            CallEdge("a.c", "a.c", "f", "g", "direct"),      # intra-file
            CallEdge("a.c", "b.c", "f", "h", "direct"),      # inter-file
        ])
        out = queries.calls(store)
        assert "a.c -> b.c" in out
        assert "a.c -> a.c" not in out
    finally:
        store.close()


# --- P2: test-path detection and C static collisions --------------------------

def test_is_test_path_matches_segments_not_substrings():
    """Both directions were wrong: `stub` claimed src/stubgen.c, and `/tests/` MISSED a
    repo-root tests/ because it demanded a leading slash. The false negative is worse —
    an unrecognised test double can win name resolution over the real implementation,
    which is the exact thing this function exists to prevent."""
    from secagent.affordances.call_map import is_test_path

    for p in ("unit-test/fm_app_tests.c", "tests/foo.c", "test/x.c", "src/mocks/bar.c",
              "unit-test/stubs/fm_cmds_stubs.c", "a/test_helper.py", "pkg/foo_test.go"):
        assert is_test_path(p), f"should be test: {p}"
    for p in ("src/stubgen.c", "src/mockingbird.c", "fsw/src/fm_cmds.c",
              "src/latest.c", "src/contest.c"):
        assert not is_test_path(p), f"should NOT be test: {p}"


def test_same_file_static_wins_over_another_files_definition():
    """Two C files may each define `static void helper(void)`. The global name->file
    table keeps only one, so whichever was indexed first captured every call to that
    name — fabricating a cross-file dependency that does not exist."""
    from secagent.affordances.call_map import (
        build_definition_table,
        build_local_definitions,
        resolve_calls,
    )
    from secagent.affordances.clang_ast import CallSite, FuncDef

    # b.c indexed FIRST — the order that used to produce the wrong answer.
    funcs = [FuncDef("helper", "", "b.c", 10), FuncDef("helper", "", "a.c", 10),
             FuncDef("a_main", "", "a.c", 20)]
    edges = resolve_calls(
        [CallSite("a_main", "helper", "a.c", 22)],
        build_definition_table(funcs),
        include_intra_file=True,
        local_defs=build_local_definitions(funcs),
    )
    assert [(e.src_file, e.dst_file) for e in edges] == [("a.c", "a.c")]


def test_cross_file_calls_still_resolve_with_local_defs():
    """The same-file preference must not break ordinary cross-file resolution."""
    from secagent.affordances.call_map import (
        build_definition_table,
        build_local_definitions,
        resolve_calls,
    )
    from secagent.affordances.clang_ast import CallSite, FuncDef

    funcs = [FuncDef("target", "", "b.c", 5), FuncDef("caller", "", "a.c", 5)]
    edges = resolve_calls(
        [CallSite("caller", "target", "a.c", 7)],
        build_definition_table(funcs),
        local_defs=build_local_definitions(funcs),
    )
    assert [(e.src_file, e.dst_file) for e in edges] == [("a.c", "b.c")]


def test_system_include_discovery_is_safe_and_cached():
    """Guards #83. A pip-installed libclang ships no libc/libc++ headers, so <cstdint>
    went unresolved and every fixed-width type collapsed to implicit `int` —
    `uint32_t calculateCRC32(uint32_t, uint8_t *, uint32_t)` was reported as
    `int calculateCRC32(int, int *, int)`, which a model then repeated as fact."""
    from secagent.affordances.clang_ast import system_include_args

    args = system_include_args()
    assert isinstance(args, tuple)
    # Either an SDK sysroot pair, explicit -isystem dirs, or nothing on a bare host —
    # never a malformed flag list.
    if args:
        assert args[0] in ("-isysroot", "-isystem")
        assert len(args) % 2 == 0
    assert system_include_args() is args          # cached, no repeated subprocess
