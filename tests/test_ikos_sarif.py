"""IKOS SARIF parsing — secagent drives IKOS with --format=sarif and reads the results."""

from __future__ import annotations

import json

from secagent.agents.analysis.ikos import parse_ikos_report, parse_sarif

_SARIF = {
    "version": "2.1.0",
    "runs": [{"results": [
        {"ruleId": "buffer-overflow", "level": "error",
         "message": {"text": "buffer overflow, accessing index 10 of 'a' of 5 elements"},
         "locations": [{"physicalLocation": {
             "artifactLocation": {"uri": "src/bug.c"},
             "region": {"startLine": 8, "startColumn": 10}}}]},
        {"ruleId": "division-by-zero", "level": "error",
         "message": {"text": "division by zero"},
         "locations": [{"physicalLocation": {
             "artifactLocation": {"uri": "src/bug.c"},
             "region": {"startLine": 10, "startColumn": 17}}}]},
        {"ruleId": "safe-check", "level": "none", "message": {"text": "ok"}},  # skipped
    ]}],
}


def test_parse_sarif_extracts_findings_with_location():
    findings = parse_sarif(_SARIF)
    assert len(findings) == 2  # the level="none" result is dropped
    boa = findings[0]
    assert boa.checker == "buffer-overflow"
    assert boa.status == "error" and boa.severity == "high"
    assert boa.file == "src/bug.c" and boa.line == 8 and boa.column == 10
    assert "index 10" in boa.message
    assert findings[1].checker == "division-by-zero" and findings[1].line == 10


# --- stacks[] -> Finding.called_from -------------------------------------------------
#
# Real IKOS SARIF output (found in cfs-build/scan-out/ikos/*.sarif and
# longrun/devF/logs/reports/unicore_ikos_sarif.json, produced by actually running IKOS
# against cFS FM and a C++ target — not synthesized to fit the parser) shows the
# enclosing-function context is carried in `results[].stacks[].frames[].location.message
# .text`, spelled exactly `"Call from <function>"`. Across 1305 real results in that
# corpus, 814 have no `stacks` key at all (a direct finding needing no call context —
# `function` must stay "" for these) and every one of the remaining 1608 stack/frame[0]
# messages matches the `"Call from "` prefix. No `logicalLocations` or `module` spelling
# was observed anywhere in that corpus, so this parser supports only the one IKOS
# actually emits.

# Trimmed from cfs-build/scan-out/ikos/cfs__apps__fm__fsw__src__fm_app.c.sarif: a
# single-frame stack — the defect (an ignored call side effect) sits directly inside
# FM_AppMain, with no further callers.
_SARIF_STACK_SINGLE_FRAME = {
    "ruleId": "ignored-call-side-effect", "level": "warning",
    "message": {"text": "ignored side effect of call to extern function 'CFE_MSG_Init'"},
    "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_app.c"},
        "region": {"startLine": 174, "startColumn": 5}}}],
    "stacks": [{"message": {"text": "."}, "frames": [
        {"location": {
            "physicalLocation": {"artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_app.c"},
                                  "region": {"startLine": 77, "startColumn": 14}},
            "message": {"text": "Call from FM_AppMain"}}},
    ]}],
}

# Trimmed from cfs-build/scan-out/ikos/cfs__apps__fm__fsw__src__fm_child.c.sarif: a
# real multi-frame call chain to the same defect (an OS_stat side effect) reached
# through FM_ChildDirListFileCmd -> FM_ChildDirListFileLoop -> FM_ChildSleepStat. IKOS
# lists frames[0] as the innermost — the function that directly contains the flagged
# statement — walking outward through callers after that.
_SARIF_STACK_MULTI_FRAME = {
    "ruleId": "ignored-call-side-effect", "level": "warning",
    "message": {"text": "ignored side effect of call to extern function 'OS_stat'"},
    "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_child.c"},
        "region": {"startLine": 1677, "startColumn": 14}}}],
    "stacks": [{"message": {"text": "."}, "frames": [
        {"location": {
            "physicalLocation": {"artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_child.c"},
                                  "region": {"startLine": 1718, "startColumn": 9}},
            "message": {"text": "Call from FM_ChildSleepStat"}}},
        {"location": {
            "physicalLocation": {"artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_child.c"},
                                  "region": {"startLine": 1580, "startColumn": 21}},
            "message": {"text": "Call from FM_ChildDirListFileLoop"}}},
        {"location": {
            "physicalLocation": {"artifactLocation": {"uri": "/cfs/apps/fm/fsw/src/fm_child.c"},
                                  "region": {"startLine": 1210, "startColumn": 13}},
            "message": {"text": "Call from FM_ChildDirListFileCmd"}}},
    ]}],
}

# Trimmed from longrun/devF/logs/reports/unicore_ikos_sarif.json: a C++ target, where
# the frame message carries a demangled member-function signature rather than a bare
# C identifier.
_SARIF_STACK_CPP_DEMANGLED = {
    "ruleId": "null-pointer-deref", "level": "warning",
    "message": {"text": "pointer '&this->0[0]' might be null"},
    "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "../src/unicore.cpp"},
        "region": {"startLine": 200, "startColumn": 14}}}],
    "stacks": [{"message": {"text": "."}, "frames": [
        {"location": {
            "physicalLocation": {"artifactLocation": {"uri": "../src/unicore.cpp"},
                                  "region": {"startLine": 92, "startColumn": 9}},
            "message": {"text": "Call from UnicoreParser::parseChar(char)"}}},
    ]}],
}

# Trimmed from the same fm_app.c.sarif file: a real result with NO `stacks` key at
# all — the common case (814/1305 results in the corpus). `function` must stay "".
_SARIF_STACK_ABSENT = {
    "ruleId": "uninitialized-variable", "level": "warning",
    "message": {"text": "variable 'MsgIdValue' might be uninitialized"},
    "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "/cfs/cfe/modules/core_api/fsw/inc/cfe_sb.h"},
        "region": {"startLine": 938, "startColumn": 29}}}],
}


def _wrap(*results):
    return {"version": "2.1.0", "runs": [{"results": list(results)}]}


# These three used to assert `function`, on the reading that frames[0] is the function
# CONTAINING the flagged statement. The frames are spelled "Call from <f>": it is the
# innermost CALLER. Checked against source on real cFS output, 10 of 10 stacked findings
# named a function that does not contain the flagged line — see
# tests/test_analysis_attribution.py, which pins the containing function against the
# source it describes. What the stack really carries is kept, under `called_from`.


def test_parse_sarif_extracts_the_caller_from_a_single_frame_stack():
    findings = parse_sarif(_wrap(_SARIF_STACK_SINGLE_FRAME))
    assert findings[0].called_from == "FM_AppMain"
    assert findings[0].function == "", "the container is resolved from the index, not here"


def test_parse_sarif_prefers_the_innermost_caller_over_the_outermost():
    # frames[0] is the INNERMOST caller — FM_ChildSleepStat's caller chain starts here —
    # and the last frame is the outermost. Taking the last would report every finding as
    # having been reached from `main`, which is true and useless.
    findings = parse_sarif(_wrap(_SARIF_STACK_MULTI_FRAME))
    assert findings[0].called_from == "FM_ChildSleepStat"


def test_parse_sarif_keeps_the_demangled_cpp_spelling():
    findings = parse_sarif(_wrap(_SARIF_STACK_CPP_DEMANGLED))
    assert findings[0].called_from == "UnicoreParser::parseChar(char)"


def test_parse_sarif_no_stacks_key_leaves_the_call_path_empty():
    # The silence case: a result with no `stacks` at all must not guess a caller from the
    # nearest symbol or the file name — it must come back empty. (`function` is filled
    # later from the index, which does know where the line is.)
    findings = parse_sarif(_wrap(_SARIF_STACK_ABSENT))
    assert findings[0].called_from == ""


def test_parse_sarif_malformed_stacks_do_not_raise_and_leave_function_empty():
    malformed_results = [
        {"ruleId": "r", "level": "warning", "message": {"text": "m"}, "stacks": []},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"}, "stacks": "not-a-list"},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": []}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": ["not-a-dict"]}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": [{"location": {}}]}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": [{"location": {"message": {}}}]}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": [{"location": {"message": {"text": ""}}}]}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [{"frames": [{"location": {"message": {"text": 123}}}]}]},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"}, "stacks": None},
        {"ruleId": "r", "level": "warning", "message": {"text": "m"},
         "stacks": [None, "not-a-dict"]},
    ]
    findings = parse_sarif(_wrap(*malformed_results))
    assert len(findings) == len(malformed_results)
    assert all(f.called_from == "" for f in findings)


def test_parse_ikos_report_detects_and_routes_sarif():
    # analyze_repo calls parse_ikos_report on the raw text; it must recognize SARIF.
    findings = parse_ikos_report(json.dumps(_SARIF))
    assert {f.checker for f in findings} == {"buffer-overflow", "division-by-zero"}


def test_compile_flags_for_extracts_includes_and_defines(tmp_path):
    from secagent.agents.analysis.ikos import compile_flags_for

    db = [{"directory": "/cfs/apps/fm", "file": "fm.c",
           "command": "gcc -I/cfs/inc -DFOO=1 -std=c99 -c fm.c -o fm.o"}]
    (tmp_path / "cc.json").write_text(json.dumps(db))
    flags = compile_flags_for(tmp_path / "cc.json", "/cfs/apps/fm/fm.c")
    assert "-I/cfs/inc" in flags and "-DFOO=1" in flags
    assert "-std=c99" not in flags  # only -I / -D / -U are kept


def test_compile_flags_for_missing_db_returns_empty(tmp_path):
    from secagent.agents.analysis.ikos import compile_flags_for

    assert compile_flags_for(tmp_path / "nope.json", "x.c") == []


def test_enumerate_functions_graceful_without_toolchain(monkeypatch, tmp_path):
    from secagent.agents.analysis import ikos

    monkeypatch.setattr(ikos.shutil, "which", lambda _b: None)  # no clang/llvm-nm
    assert ikos.enumerate_functions(tmp_path / "x.c") == []


def test_iter_compile_units_dedupes_and_filters(tmp_path):
    from secagent.agents.analysis.ikos import iter_compile_units

    db = [
        {"directory": "/r/a", "file": "x.c", "command": "gcc -I/r/inc -DFOO -c x.c"},
        {"directory": "/r/a", "file": "x.c", "command": "gcc -I/r/inc -DFOO -c x.c"},  # dup
        {"directory": "/r/a", "file": "y.h", "command": "gcc -c y.h"},                 # header
        {"directory": "/r/b", "file": "z.cpp", "arguments": ["clang", "-I/r/z", "-c", "z.cpp"]},
    ]
    (tmp_path / "cc.json").write_text(json.dumps(db))
    units = list(iter_compile_units(tmp_path / "cc.json"))
    by_name = {f.split("/")[-1]: fl for f, fl in units}
    assert set(by_name) == {"x.c", "z.cpp"}  # x.c deduped, y.h skipped
    assert "-I/r/inc" in by_name["x.c"] and "-DFOO" in by_name["x.c"]
    assert "-I/r/z" in by_name["z.cpp"]


def test_discover_compile_db_prefers_largest(tmp_path):
    from secagent.agents.analysis.agent import _discover_compile_db

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "compile_commands.json").write_text(
        json.dumps([{"file": "a.c", "directory": ".", "command": "gcc -c a.c"}]))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "compile_commands.json").write_text(
        json.dumps([{"file": "a.c"}, {"file": "b.c"}]))
    assert _discover_compile_db(tmp_path).endswith("sub/compile_commands.json")
