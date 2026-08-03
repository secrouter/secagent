"""End-to-end regression harness for the behaviours two agentic test cohorts broke.

The unit tests cover each fix in isolation. These run the REAL pipeline — index a small
C project, then query it through the affordance layer — because the failures that
actually cost developers time were emergent: a degraded parse silently dropping most of
the call graph, a display field used as a retrieval index, a truncated list that looked
complete. Those only show up end to end.

Each test names the fix it guards so a regression points straight at the change that
caused it. Everything runs heuristic-only (no LLM) and takes ~1s.

The fixture (`tests/fixtures/c_app`) deliberately mirrors the cFS shapes that broke:
a NASA-style license block whose first line carries no license keyword, Doxygen `@file`
headers, a macro-only header, `typedef struct` message types, a dispatch switch calling a
handler in another file through a cast, and a same-file helper call.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from secagent.affordances import queries
from secagent.affordances.api import index_repo
from secagent.affordances.clang_ast import clang_available
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "c_app"

# The call map needs libclang; the regex fallback yields symbols but no call edges.
needs_clang = pytest.mark.skipif(
    not clang_available(), reason="libclang not installed (pip install 'secagent[clang]')"
)


def _settings(store_dir: Path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False        # heuristic only: fast + deterministic
    s.affordances.store_dir = str(store_dir)
    return s


def _index(root: Path, store_dir: Path) -> AffordanceStore:
    index_repo(root, _settings(store_dir))
    return AffordanceStore(root, str(store_dir))


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """The fixture project, indexed once. Copied out so .secagent never lands in the repo."""
    base = tmp_path_factory.mktemp("c_app")
    root = base / "app"
    shutil.copytree(FIXTURE, root)
    store = _index(root, base / "store")
    yield store
    store.close()


@pytest.fixture(scope="module")
def app_missing_header(tmp_path_factory):
    """Same project plus a file including a header that does not exist.

    Reproduces the state that made `callers` answer "No callers found" for live code in
    cohort 1 — a degraded parse, which must now announce itself.
    """
    base = tmp_path_factory.mktemp("c_app_degraded")
    root = base / "app"
    shutil.copytree(FIXTURE, root)
    (root / "src" / "app_broken.c").write_text(
        '#include "definitely_not_here.h"\nvoid AppBroken(void) {}\n', encoding="utf-8"
    )
    store = _index(root, base / "store")
    yield store
    store.close()


# --- #68: license blocks are not a file's purpose ----------------------------------

def test_purpose_is_the_description_not_the_license(app):
    """Guards #68. Every file used to report "NASA Docket No"."""
    for path, expected in (
        ("src/app_cmds.c", "Fixture App ground command handlers"),
        ("inc/app_msgids.h", "Fixture App message IDs"),
    ):
        summary = app.load_summary(path)
        assert summary is not None, f"{path} not indexed"
        assert summary.purpose == expected, f"{path}: {summary.purpose!r}"
        for junk in ("NASA Docket", "Copyright", "Apache", "@file"):
            assert junk not in summary.purpose


# --- #75: header macros and typedefs are findable ----------------------------------

@pytest.mark.parametrize(
    ("symbol", "kind", "file"),
    [
        ("APP_GET_FILE_INFO_CC", "constant", "inc/app_msgstruct.h"),
        ("APP_FILE_INFO_TLM_MID", "constant", "inc/app_msgids.h"),
        ("AppGetFileInfoCmd_t", "class", "inc/app_msgstruct.h"),
        ("AppId_t", "class", "inc/app_msgstruct.h"),
    ],
)
def test_find_symbol_resolves_header_declarations(app, symbol, kind, file):
    """Guards #75. In C flight software the command codes and message types ARE the
    wiring, and they live only in headers; find-symbol used to answer "No symbols
    matching" — worded identically to "does not exist"."""
    out = json.loads(queries.find_symbol(app, symbol))
    assert isinstance(out, list), f"{symbol}: not found ({out})"
    hit = next((h for h in out if h["name"] == symbol), None)
    assert hit is not None, f"{symbol}: absent from {out}"
    assert hit["kind"] == kind and hit["path"] == file


def test_include_guards_are_not_indexed(app):
    """Guards #75's filter: `#define APP_MSGIDS_H` is structure, not API."""
    out = json.loads(queries.find_symbol(app, "APP_MSGIDS_H"))
    assert isinstance(out, dict) and "note" in out


# --- #69: cross-file call edges survive (the cFS dispatch idiom) --------------------

@needs_clang
def test_callers_finds_the_cross_file_dispatch_caller(app):
    """Guards #69. `AppProcessGroundCommand` calls the handler in another file through a
    cast — the exact shape whose disappearance made a dev conclude live code was dead."""
    out = json.loads(queries.callers(app, "AppGetFileInfoCmd"))
    assert isinstance(out, list), f"no callers found: {out}"
    assert any(
        c["path"] == "src/app_dispatch.c" and c["caller"] == "AppProcessGroundCommand"
        for c in out
    ), out


# --- #73: same-file callers are reported -------------------------------------------

@needs_clang
def test_callers_finds_the_same_file_caller(app):
    """Guards #73. "Who calls X" is a question about a symbol, not about files."""
    out = json.loads(queries.callers(app, "AppReportCount"))
    assert isinstance(out, list), f"no callers found: {out}"
    assert any(
        c["path"] == "src/app_cmds.c" and c["caller"] == "AppGetFileInfoCmd" for c in out
    ), out


@needs_clang
def test_callers_reports_the_real_call_site_line(app):
    """"Who calls X, now show me the call site" must be two calls, not two-and-a-guess.

    Every producer already captured the call-site line — clang's `CallSite.line`, the
    tree-sitter backends' `node.start_point[0] + 1`, the heavy backend's
    `AnalysisCall.line` — and `resolve_calls` dropped it on the way into the store, so
    the second hop needed `find_symbol` on the CALLER and a guess about which of its
    lines held the call.

    The assertion reads the fixture's own source at the reported line and requires the
    callee to appear there. A hardcoded number would pass just as well against a line
    that is confidently wrong, and a wrong location is worse than an absent one: it
    sends a reader somewhere specific. This is also why the edge comes from a real
    `index_repo` run rather than a hand-built `CallEdge` — a fabricated edge would
    prove only that the plumbing copies a number.
    """
    for symbol, path, caller in (
        ("AppReportCount", "src/app_cmds.c", "AppGetFileInfoCmd"),
        ("AppGetFileInfoCmd", "src/app_dispatch.c", "AppProcessGroundCommand"),
    ):
        out = json.loads(queries.callers(app, symbol))
        row = next((c for c in out if c["path"] == path and c["caller"] == caller), None)
        assert row is not None, f"{symbol}: {caller} missing from {out}"
        assert "line" in row, f"{symbol}: a freshly indexed store must carry the line"

        source = (app.repo_root / path).read_text(encoding="utf-8").splitlines()
        assert 1 <= row["line"] <= len(source), f"{symbol}: line {row['line']} is off-file"
        assert symbol in source[row["line"] - 1], (
            f"{symbol}: reported line {row['line']} is {source[row['line'] - 1]!r}, "
            "which does not contain the call"
        )


@needs_clang
def test_calls_map_stays_file_to_file(app):
    """Guards #73's other half: storing intra-file edges must not pollute the map."""
    out = queries.calls(app)
    assert "src/app_dispatch.c -> src/app_cmds.c" in out
    assert "src/app_cmds.c -> src/app_cmds.c" not in out


# --- #69 + #71: the fidelity warning fires when broken, and ONLY then ---------------

@needs_clang
def test_no_fidelity_warning_when_the_parse_is_healthy(app):
    """Guards #71. A warning that always fires is one nobody reads — this half is what
    #69 originally got wrong, crying wolf on every healthy parse because a pip-installed
    libclang has no libc headers."""
    out = json.loads(queries.callers(app, "NoSuchFunction"))
    note = out["note"] if isinstance(out, dict) else ""
    assert "INCOMPLETE" not in note and "WARNING" not in note, note


@needs_clang
def test_fidelity_warning_fires_on_a_missing_project_header(app_missing_header):
    """Guards #69. An empty result must not read as "there are no callers"."""
    out = json.loads(queries.callers(app_missing_header, "NoSuchFunction"))
    note = out["note"]
    assert "INCOMPLETE" in note
    assert "does NOT prove there are no callers" in note
    assert "definitely_not_here.h" in note      # names the actionable cause


@needs_clang
def test_parse_health_counts_only_the_broken_file(app_missing_header):
    """Guards #71's precision: one bad file, not "28 of 28"."""
    health = app_missing_header.parse_health()
    assert health["degraded"] == 1, health
    assert health["total"] >= 3


# --- #74: truncation announces itself ----------------------------------------------

def test_key_symbols_reports_what_it_hid(tmp_path):
    """Guards #74. Both cohort-2 devs read a capped list as a file's complete API."""
    from secagent.affordances.file_summary import _MAX_KEY_SYMBOLS, summarize_file
    from secagent.affordances.models import Symbol

    syms = [Symbol(f"fn{i}", "function", "a.c", i, f"void fn{i}(void)") for i in range(20)]
    summary = summarize_file("a.c", "int x;\n", "C", syms)
    assert len(summary.key_symbols) == _MAX_KEY_SYMBOLS + 1
    assert "+8 more" in summary.key_symbols[-1]


# --- #77: search ranks on the real symbol index ------------------------------------

def test_search_finds_a_file_by_a_macro_it_defines(app):
    """Guards #77. `inc/app_msgids.h` has key_symbols == [] (all its symbols are
    constants); ranking on that field made it invisible to search entirely."""
    out = json.loads(queries.search(app, "APP_FILE_INFO_TLM_MID"))
    assert out and out[0]["path"] == "inc/app_msgids.h", out


def test_search_prefers_the_defining_file_over_mere_mentions(app):
    """Guards #77. devD's real query: the generic words used to outvote the one term
    that identifies the file uniquely."""
    out = json.loads(queries.search(app, "APP_FILE_INFO_TLM_MID macro definition"))
    assert out and out[0]["path"] == "inc/app_msgids.h", out


def test_context_shows_symbols_for_a_constants_only_file(app):
    """Guards #77's other half: such a file would rank correctly and then display no
    symbols, because the context block also read key_symbols."""
    from secagent.affordances.retrieval import ContextBuilder
    from secagent.llm.tokenizer import TokenCounter

    text = ContextBuilder(app, 4096, TokenCounter()).for_query("APP_FILE_INFO_TLM_MID")
    assert "inc/app_msgids.h" in text
    assert "APP_FILE_INFO_TLM_MID" in text


# --- #82: C++ methods are indexed and their calls resolve ---------------------

@needs_clang
def test_cpp_methods_are_indexed_with_qualified_names(tmp_path_factory):
    """Guards #82. clang's walker handled only FUNCTION_DECL, so a C++ codebase yielded
    almost nothing: both run-3 devs found `functions` empty on a file with 9 methods."""
    from secagent.affordances.clang_ast import parse_file

    root = tmp_path_factory.mktemp("cpp")
    (root / "w.h").write_text(
        "class Widget {\npublic:\n  Widget();\n  void run();\n  int value() const;\n};\n")
    (root / "w.cpp").write_text(
        '#include "w.h"\n'
        "Widget::Widget() {}\n"
        "void Widget::run() { value(); }\n"
        "int Widget::value() const { return 1; }\n"
    )
    unit = parse_file(root / "w.cpp", root)
    names = {f.name for f in unit.functions}
    assert "Widget::run" in names and "Widget::value" in names
    assert "Widget::Widget" in names                      # constructor
    # Callees are qualified too, or method calls would never match their definitions.
    assert ("Widget::run", "Widget::value") in {(c.caller, c.callee) for c in unit.calls}
