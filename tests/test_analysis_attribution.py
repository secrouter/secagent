"""Where a finding IS, and how IKOS got there, are two different facts.

`Finding.function` was filled from SARIF `stacks[]` on the reading that `frames[0]` is the
function containing the flagged statement. The frames are spelled "Call from <f>", so it
is a CALLER — one level out from the defect. Checked against source, 10 of 10 stacked
findings on real cFS output named a function that does not contain the flagged line.

These tests run against real IKOS SARIF and the real source it describes
(`tests/fixtures/ikos_fm/`). A synthetic fixture would have encoded the same misreading
that produced the bug and passed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secagent.affordances.api import index_repo
from secagent.affordances.store import AffordanceStore
from secagent.agents.analysis.agent import _enrich
from secagent.agents.analysis.ikos import parse_sarif
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "ikos_fm"

# Read from fm_cmd_utils.c: `grep -n '^[A-Za-z_].*(' | grep -v ';'`. Every line below is
# the real containing function for the findings at those lines.
#   FM_GetFilenameState  135-219
#   FM_VerifyNameValid   220-246
#   FM_VerifyFileState   247-363
CONTAINING = {
    147: "FM_GetFilenameState", 149: "FM_GetFilenameState", 166: "FM_GetFilenameState",
    180: "FM_GetFilenameState", 191: "FM_GetFilenameState", 192: "FM_GetFilenameState",
    193: "FM_GetFilenameState", 346: "FM_VerifyFileState", 347: "FM_VerifyFileState",
}


@pytest.fixture
def analysed(tmp_path):
    """Real SARIF, parsed and enriched against a real index of the real source."""
    repo = tmp_path / "fm"
    (repo / "fsw" / "src").mkdir(parents=True)
    (repo / "fsw" / "src" / "fm_cmd_utils.c").write_text(
        (FIXTURE / "fm_cmd_utils.c").read_text())
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    index_repo(repo, s)
    findings = parse_sarif(json.loads((FIXTURE / "ikos-report.sarif").read_text()))
    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        _enrich(findings, store, repo)
    finally:
        store.close()
    return findings


def _stacked(findings):
    return [f for f in findings if f.called_from]


def test_function_names_the_function_that_contains_the_line(analysed):
    """The bug, stated as the property that was false: every finding's `function` must be
    the function whose body the flagged line is in — checked against the source, not
    against the stack that produced the wrong answer."""
    seen: set[int] = set()
    for f in analysed:
        want = CONTAINING.get(f.line)
        if want is None:
            continue
        assert f.function == want, (
            f"{f.file}:{f.line} is inside {want}, reported as {f.function!r}")
        seen.add(f.line)          # line 149 carries two findings; count lines, not rows
    assert seen == set(CONTAINING), f"lines not present in the fixture: {set(CONTAINING) - seen}"


def test_the_call_path_is_kept_under_its_own_name(analysed):
    """The stack is genuinely useful — it says how IKOS reached the defect. Renaming the
    bug by deleting it would have thrown that away. It moves to `called_from`."""
    stacked = _stacked(analysed)
    assert len(stacked) == 10, "all ten stacked results must still carry their call path"
    by_line = {f.line: f.called_from for f in stacked}
    assert by_line[166] == "FM_VerifyFileState"
    assert by_line[191] == "FM_VerifyNameValid"
    assert by_line[346] == "FM_VerifyDirExists"


def test_the_caller_is_never_silently_reported_as_the_container(analysed):
    """Attack the mechanism. The two fields must not agree on any stacked finding here:
    IKOS only emits a stack when it entered the containing function from somewhere else,
    so `called_from == function` would mean the old value had been copied across."""
    for f in _stacked(analysed):
        assert f.function != f.called_from, (
            f"{f.file}:{f.line} reports its caller as its container again")


def test_findings_with_no_stack_still_get_a_containing_function(analysed):
    """Paired silence, and the reason to derive this from the index rather than the stack.

    814 of 1305 real IKOS results carry no `stacks[]` at all, so the old approach left
    `function` empty for most findings — including every finding in a file IKOS never
    called into. `file:line` plus the symbol index knows the answer regardless.
    """
    unstacked = [f for f in analysed if not f.called_from]
    assert unstacked, "the fixture keeps six results with no stacks, on purpose"
    assert all(f.function for f in unstacked), "an anchored line always has a container"


def test_a_line_before_any_function_gets_no_function(tmp_path):
    """Paired silence: no nearest-preceding-function guess when there is none. A finding
    in a header's declarations must not be attributed to whatever is defined after it."""
    from secagent.agents.analysis.agent import _containing_function

    starts = [(135, "FM_GetFilenameState"), (247, "FM_VerifyFileState")]
    assert _containing_function(starts, 10) == ""
    assert _containing_function(starts, 135) == "FM_GetFilenameState"
    assert _containing_function(starts, 246) == "FM_GetFilenameState"
    assert _containing_function(starts, 247) == "FM_VerifyFileState"
    assert _containing_function([], 100) == ""


# --------------------------------------------------------------------------------------
# Entry-point enumeration: a Clang ctor/dtor alias silently costs a whole file.
# --------------------------------------------------------------------------------------

# Real `llvm-nm-14 --defined-only` output for PX4's `src/rtcm.cpp`, compiled with
# clang-14. `RTCMParsing` has no virtual bases, so `-mconstructor-aliases` (Clang's
# default) emits C1/D1 as LLVM GlobalAliases to C2/D2 — and `nm` prints all four as `T`,
# indistinguishable. Confirmed with `llvm-dis-14`:
#   @_ZN11RTCMParsingC1Ev = dso_local unnamed_addr alias ... @_ZN11RTCMParsingC2Ev
#   @_ZN11RTCMParsingD1Ev = dso_local unnamed_addr alias ... @_ZN11RTCMParsingD2Ev
_RTCM_NM = """\
---------------- T _ZN11RTCMParsing5crc24EPKht
---------------- T _ZN11RTCMParsing5resetEv
---------------- T _ZN11RTCMParsing7addByteEh
---------------- T _ZN11RTCMParsingC1Ev
---------------- T _ZN11RTCMParsingC2Ev
---------------- T _ZN11RTCMParsingD1Ev
---------------- T _ZN11RTCMParsingD2Ev
"""


def test_a_constructor_alias_is_replaced_by_the_body_it_points_at():
    """IKOS's entry-point resolver walks real functions, not aliases, so it refuses
    `C1Ev` and aborts the ENTIRE translation unit before analysing any other entry point.

    Measured on this exact file with IKOS 3.4:
        entry points as shipped (C1/D1 present) -> exit 9, 0 findings
        the same list with C1->C2 and D1->D2    -> exit 0, 116 findings

    So `rtcm.cpp` lost 100% of its findings, silently, and every stateful C++ class with a
    non-trivial constructor hits it.
    """
    from secagent.agents.analysis.ikos import defined_function_symbols

    entry_points, aliased = defined_function_symbols(_RTCM_NM)

    assert "_ZN11RTCMParsingC1Ev" not in entry_points, "IKOS cannot resolve an alias"
    assert "_ZN11RTCMParsingD1Ev" not in entry_points
    assert "_ZN11RTCMParsingC2Ev" in entry_points, "the body must still be analysed"
    assert "_ZN11RTCMParsingD2Ev" in entry_points
    assert set(aliased) == {"_ZN11RTCMParsingC1Ev", "_ZN11RTCMParsingD1Ev"}
    # The three ordinary methods are untouched.
    assert "_ZN11RTCMParsing7addByteEh" in entry_points


def test_a_complete_object_constructor_with_no_sibling_is_kept():
    """Paired silence, and the reason the substitution is confirmed against the symbol
    table rather than parsed out of the mangling. When no base-object sibling exists, C1
    IS the body — dropping it would be the coverage loss this fix exists to prevent."""
    from secagent.agents.analysis.ikos import defined_function_symbols

    entry_points, aliased = defined_function_symbols(
        "---------------- T _ZN4SoloC1Ev\n"
        "---------------- T _ZN4Solo3runEv\n"
    )
    assert entry_points == ["_ZN4SoloC1Ev", "_ZN4Solo3runEv"]
    assert aliased == []


def test_weak_symbols_are_entry_points():
    """The narrowing that shipped for this bug bought nothing, and this pins its removal.

    It held back weak symbols on the theory that IKOS might not emit them and would abort.
    Measured with IKOS 3.4 and clang-14: a TU with a `= default` ctor, an inline member
    and a template instantiation emits `_ZNK5Thing5twiceEv` and `_Z5addupIiET_S0_S0_` as
    `W`; passing both to `ikos -e` exits 0 and returns real findings inside them. And
    across the three analysable PX4 driver TUs there are ZERO weak symbols, so the filter
    never fired on the corpus whose failure motivated it. It only cost the coverage of
    every inline and template function in a header.
    """
    from secagent.agents.analysis.ikos import defined_function_symbols

    entry_points, aliased = defined_function_symbols(
        "---------------- T _Z3useP5Thing\n"
        "---------------- W _Z5addupIiET_S0_S0_\n"
        "---------------- W _ZNK5Thing5twiceEv\n"
    )
    assert entry_points == ["_Z3useP5Thing", "_Z5addupIiET_S0_S0_", "_ZNK5Thing5twiceEv"]
    assert aliased == []


def test_data_symbols_are_still_not_entry_points():
    """Paired silence: widening to weak TEXT must not admit weak DATA. `V` is a weak
    object and `D`/`B` are data — none of them are functions."""
    from secagent.agents.analysis.ikos import defined_function_symbols

    entry_points, _ = defined_function_symbols(
        "---------------- T FM_AppMain\n"
        "---------------- V _ZN4Rtcm5tableE\n"
        "---------------- D some_global\n"
        "---------------- B another_global\n"
    )
    assert entry_points == ["FM_AppMain"]


def test_the_cfs_framework_is_vendored_in_full():
    """`cFS/cfe/...` tied with app code at rank 0, and the alphabetical tiebreak put it
    first ("c" < "f"), so four of ten triage slots on `fm_dispatch.c` went to `cfe_sb.h`.
    `osal` and `psp` were listed and `cfe` — the third sibling, and the one every cFS app
    includes — was not."""
    from secagent.affordances.priority import is_vendored, path_rank

    assert is_vendored("cFS/cfe/modules/core_api/fsw/inc/cfe_sb.h")
    assert is_vendored("cFS/osal/src/os/inc/osapi-idmap.h")
    assert is_vendored("cFS/psp/fsw/inc/cfe_psp.h")
    assert path_rank("fsw/src/fm_dispatch.c") < path_rank(
        "cFS/cfe/modules/core_api/fsw/inc/cfe_sb.h")


def test_an_app_named_for_the_framework_is_not_vendored():
    """Paired silence: `cfe` is a DIRECTORY segment test, so a user's own file that merely
    mentions the framework in its name stays their code."""
    from secagent.affordances.priority import is_vendored

    assert not is_vendored("fsw/src/fm_cfe_helpers.c")
    assert not is_vendored("apps/fm/fsw/src/cfe_shim.c")
