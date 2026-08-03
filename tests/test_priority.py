"""Bounded selections must choose by importance, never by path order.

Four commands independently used "whichever sorts first" and all four were wrong the same
way: analysis triage spent its whole budget on vendored `cFS/osal/` headers (they sort
before `fsw/src/`), `testgen` and the docs describe pass spent theirs on `examples/*`, and
`scan` inherited the same shape from its walk.

Each was fixed separately, with its own segment list. The point of this module is that the
fifth command should not have to rediscover it — so the important test here is not the
four behavioural ones, it is `test_every_bounded_selection_sorts_first`, which fails for
code that does not exist yet.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from secagent.affordances.priority import is_demo, is_vendored, path_rank

SRC = Path(__file__).resolve().parents[1] / "src" / "secagent"
RULES = Path(__file__).resolve().parents[1] / "config" / "rules" / "embedded-cpp.yaml"


# --- the ranking itself -------------------------------------------------------

def test_library_code_outranks_examples_and_vendored():
    paths = ["examples/demo.py", "vendor/lib.c", "src/click/core.py", "tests/test_x.py"]
    assert sorted(paths, key=path_rank)[0] == "src/click/core.py"


def test_the_reported_cases():
    """The four bugs, as data."""
    assert path_rank("fsw/src/fm_cmds.c") < path_rank("cFS/osal/src/os-impl.c")
    assert path_rank("src/click/core.py") < path_rank("examples/complex/demo.py")
    assert path_rank("src/lib.rs") < path_rank("benches/bench.rs")
    assert path_rank("src/parser.cpp") < path_rank("build/generated.cpp")


def test_a_filename_that_merely_mentions_tests_is_still_library_code():
    """`src/test_helpers.c` is library code named for tests; `tests/helpers.c` is not.
    Matching the filename as well as the directories would demote the first."""
    assert not is_demo("src/test_helpers.c")
    assert is_demo("tests/helpers.c")


def test_ordering_is_deterministic():
    """A bounded result that cannot be compared with itself is not a result."""
    paths = ["b/x.c", "a/y.c", "examples/z.c", "vendor/w.c"]
    shuffled = [paths[2], paths[0], paths[3], paths[1]]
    assert sorted(paths, key=path_rank) == sorted(shuffled, key=path_rank)


def test_vendored_is_not_confused_with_demo():
    assert is_vendored("node_modules/pkg/index.js")
    assert not is_demo("node_modules/pkg/index.js")


def test_ordinary_paths_are_neither():
    for p in ("src/main.c", "lib/parser.rs", "app/Program.cs"):
        assert not is_vendored(p) and not is_demo(p), p


# --- the test that catches the command that does not exist yet ----------------

# Every `max_*` setting is one of two things, and confusing them is what made the first
# version of this check useless: it flagged `text[:max_bytes]` (truncating one file's
# CONTENT, where ordering is meaningless) while missing scan's real selection, which used
# a local variable. A check that cries wolf gets disabled by the first person it annoys.
#
# SELECTION settings choose WHICH items to spend a budget on — every one of the five bugs
# lived here. TRUNCATION settings cut a single item down to size; there is nothing to rank.
SELECTION_SETTINGS = {
    "max_files", "max_unit_files", "max_functional_components", "max_triage",
    "max_context_files", "max_function_docs", "max_findings_per_file",
}
TRUNCATION_SETTINGS = {
    "max_file_bytes", "max_tokens", "max_output_tokens", "max_lines", "max_retries",
    "max_context_length",
}


def test_every_max_setting_is_classified():
    """A new `max_*` setting must be declared selection or truncation.

    This is the safeguard that actually catches the next command: the author of a new
    budget cannot add it without answering "what is this budget FOR", which is the
    question all five bugs failed to ask.
    """
    from secagent.config import Settings

    found = set()
    for _name, field in Settings.model_fields.items():
        model = field.annotation
        if hasattr(model, "model_fields"):
            found |= {f for f in model.model_fields if f.startswith("max_")}
    unclassified = found - SELECTION_SETTINGS - TRUNCATION_SETTINGS
    assert not unclassified, (
        f"classify these as selection (rank before capping) or truncation: {unclassified}"
    )


# A module that merely PASSES a budget along (cli wiring, tool registration) is not
# making the choice, so requiring it to rank would be noise — the failure mode this
# whole file exists to avoid. Only a segment that actually truncates a sequence is
# deciding which items survive.
#
# The bound must contain a NAME, not just digits. A budget always arrives through a
# variable or an attribute — `[:cap]`, `[:max_files]`, `[:cfg.max_unit_files]`,
# `[:ruleset.max_findings_per_file]` — never as a literal, and every real selection in
# `src/` is of that shape. A literal bound is something else entirely: `[:3]` on a
# failures dict is a log sample, `[:-1]` trims a `Z` off a timestamp. Matching those too
# is what put `scan_repo` on the allowlist, and an allowlist entry is a blind spot with a
# name on it — while a function is listed, a REAL unranked selection added to it later is
# invisible. Retiring an entry by sharpening the rule is strictly better than exempting
# it. (This also kills the `output.py` false alarm at the root, independently of the
# string-literal stripping below: the two fix different halves of the same wolf-cry.)
_SLICE_RE = re.compile(r"\[\s*:\s*[^\]]*[A-Za-z_][^\]]*\]")

# Part 3(a): `getattr(cfg.scan, "max_files")` spends a budget through a setting name that
# lives only in a string literal — `_code_only` strips it on purpose (that stripping is
# what stops a docstring's mention of `scan.max_files` from reading as a reference), so
# this narrow, separate scan is the only thing that still sees it. Deliberately anchored
# to `getattr(...,  "max_*")` rather than any string — a wide match would resurrect the
# prose-matching false alarm the `_code_only` narrowing was built to kill.
_GETATTR_SETTING_RE = re.compile(r"""getattr\([^,]+,\s*["'](max_\w*)["']""")


def _code_only(source: str) -> str:
    """Return ``source`` with comments and string literals removed.

    A budget is *spent* through an identifier or an attribute (`cfg.max_files`); prose
    that merely mentions a setting is not a selection. Used ONLY to decide whether a
    segment references a setting — every other check below still runs against the raw
    source, so this narrows what counts as a reference without relaxing anything the
    check demands once it finds one.

    On a file this cannot tokenize, it returns the source unchanged: a parse problem
    must not be a way for a module to slip past the check.

    Part 3(b): on Python >=3.12 an f-string's literal text tokenizes as FSTRING_MIDDLE
    (with FSTRING_START/FSTRING_END bracketing the expression parts), not a single
    STRING token — so `f"skipped by scan.max_files={n}"` used to leak `max_files`
    through untouched on those interpreters, the exact false alarm the STRING skip
    exists to prevent for an ordinary string. Looked up via ``getattr`` so this still
    runs on interpreters (<3.12) that don't have these token types at all.
    """
    import io
    import tokenize

    skip = {tokenize.COMMENT, tokenize.STRING}
    for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        _tok_type = getattr(tokenize, _name, None)
        if _tok_type is not None:
            skip.add(_tok_type)

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type not in skip:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError):   # pragma: no cover - defensive
        return source
    return "\n".join(out)


def _segments(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(label, source)`` pairs the guard checks independently:
    one per top-level ``def``, one per method of each top-level ``class``, and one
    ``"<module>"`` bucket for everything else (imports, module-level code, a class's
    non-method body).

    A nested function is deliberately NOT split out on its own: `ast.get_source_segment`
    on the enclosing def returns its whole body text, nested defs included, so a sort
    inside a nested helper still counts for the function that contains it — the module
    docstring's "a nested `_one` belongs to its enclosing function's scope" rule.

    A file this cannot parse contributes one ``"<module>"`` segment covering the whole
    file — the same fail-closed shape ``_code_only`` uses for a tokenize failure; a
    parse problem must not be a way to slip past the check either.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [("<module>", text)]

    segments: list[tuple[str, str]] = []
    covered: set[int] = set()

    def take(node: ast.AST) -> str:
        start, end = node.lineno, node.end_lineno
        if start and end:
            covered.update(range(start, end + 1))
        return ast.get_source_segment(text, node) or ""

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segments.append((node.name, take(node)))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    segments.append((f"{node.name}.{item.name}", take(item)))

    lines = text.splitlines(keepends=True)
    leftover = "".join(ln for i, ln in enumerate(lines, start=1) if i not in covered)
    segments.append(("<module>", leftover))
    return segments


def _segment_offenders(label: str, text: str) -> dict[str, list[str]]:
    """Run the selection-budget checks within each function-level segment of ``text``
    (see `_segments`) instead of over the whole module.

    Module granularity was the guard's blind spot: one ranked selection anywhere in a
    file — `path_rank`/`sort(`/`sorted(` appearing ANYWHERE in the text — used to exempt
    every OTHER selection in that same file, even an unrelated one with no ranking of
    its own. Segmenting by function means a rank in one function no longer vouches for
    a sibling function that has none.

    Returns ``{"<label>::<segment>": sorted(settings referenced)}`` for every segment
    that spends a SELECTION budget (referenced + sliced) without ranking it.
    """
    offenders: dict[str, list[str]] = {}
    for seg_label, seg_text in _segments(text):
        # Reference = named in CODE (not prose, not a comment — see `_code_only`) OR
        # named as a `getattr(..., "max_*")` string key (Part 3(a) — `_code_only`
        # strips string literals on purpose, so that shape needs its own narrow scan).
        used = {s for s in SELECTION_SETTINGS if s in _code_only(seg_text)}
        used |= {m for m in _GETATTR_SETTING_RE.findall(seg_text) if m in SELECTION_SETTINGS}
        if not used or not _SLICE_RE.search(seg_text):
            continue
        if "path_rank" in seg_text or "sort(" in seg_text or "sorted(" in seg_text:
            continue
        offenders[f"{label}::{seg_label}"] = sorted(used)
    return offenders


# Functions the per-function pass below flags that are NOT fixed in this PR — either a
# real gap tracked as a follow-up, or a false positive the finer granularity introduces
# by severing a link the mechanical checks cannot see across (ranking that happens in a
# helper the flagged function calls, or a slice that sits next to a setting reference
# without being the selection cut itself). Every entry needs a real, specific reason.
#
# The assertion below is `offenders == KNOWN_UNRANKED`, not `not offenders`: a NEW
# unranked selection fails (it is not in this dict), and a FIXED one also fails (it is
# in this dict but the guard no longer flags it) until someone removes it from here.
# That second half is what stops this list from becoming a place bugs go to retire.
KNOWN_UNRANKED = {
    "agents/testgen/agent.py::_gen_functional": (
        "real gap, not fixed here: `components[: cfg.max_functional_components]` has no "
        "ranking. Components are named units with no path to rank by — fixing this needs "
        "a decision on what 'important' means for a component (most member files? most "
        "IO edges? most recently changed?), which is a design question, not a one-line "
        "port of `path_rank`. Tracked as a follow-up."
    ),
    "affordances/retrieval.py::ContextBuilder.for_query": (
        "false positive: `ranked = self.rank_summaries(query, ...)[:limit]` slices the "
        "ALREADY-ranked return value of `rank_summaries`, which does its own relevance "
        "`scored.sort(key=lambda x: (-x[0], x[1].path))` — the rank and the slice are in "
        "two different methods of the same class, which per-function granularity cannot "
        "see across without knowing `rank_summaries`'s contract. Not a selection bug."
    ),
}
# `agents/scan/agent.py::scan_repo` used to be listed here. It was never an unranked
# selection: it names `settings.scan.max_files` only in a cost-warning log line, and its
# `[:3]`s are diagnostic samples of the failures/partial dicts. It appeared because the
# slice pattern accepted a literal bound. Requiring a name in the bound (see `_SLICE_RE`)
# retires it honestly, which is worth more than the exemption was — a listed function is
# one where the next real bug would be invisible.


def test_every_bounded_selection_sorts_first():
    """Every function-level segment that spends a SELECTION budget must rank it first.

    The four per-command tests below only prove the four known bugs are fixed; this one
    is what catches the next command that doesn't exist yet, at FUNCTION granularity —
    see `_segment_offenders` for why module granularity was not enough (an adversarial
    review proved it: `analyze_scan`'s unranked `iter_compile_units` cap hid in the same
    module as `_triage`'s `sorted(findings, key=_triage_order)`, and a second probe showed
    that deleting scan's `eligible.sort(key=path_rank)` alone, leaving the unrelated
    `out.sort(...)` in `parse_findings`, left the module-level version of this check
    silent — reproduced directly in `test_guard_catches_removing_one_sort_...` below).

    Anything flagged here that is not fixed in this PR is named in `KNOWN_UNRANKED`
    with a real, specific reason.
    """
    offenders: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        offenders.update(_segment_offenders(str(path.relative_to(SRC)), text))
    # `offenders` maps label -> the settings it spends (what the guard found);
    # `KNOWN_UNRANKED` maps label -> why it is not fixed here (what a human decided).
    # Different value shapes on purpose, so the comparison is by KEY: a NEW unranked
    # selection adds a label `KNOWN_UNRANKED` does not have, and a FIXED one removes a
    # label `KNOWN_UNRANKED` still has — either way the two label sets stop matching
    # until a human updates the allowlist, which is the whole point of `==` over
    # `not offenders`.
    new = offenders.keys() - KNOWN_UNRANKED.keys()
    resolved = KNOWN_UNRANKED.keys() - offenders.keys()
    assert not new and not resolved, (
        "these functions spend a selection budget without deciding what it is for, so it "
        f"goes to whatever sorts first — NEW (add to KNOWN_UNRANKED or fix): "
        f"{ {k: offenders[k] for k in new} }; "
        f"KNOWN_UNRANKED entries no longer flagged (remove from the allowlist): "
        f"{sorted(resolved)}"
    )


# --- the guard mechanism, attacked directly -----------------------------------

def test_guard_catches_removing_one_sort_when_a_sibling_function_still_sorts():
    """Reviewer's Probe A, reproduced in a SYNTHETIC module (not real source): two
    independent selections live in the same file. One is ranked; the other lost its
    `eligible.sort(key=path_rank)` — e.g. someone "simplified" it in review and the
    import stayed behind. A module-granular check that merely asks "does
    `path_rank`/`sort(`/`sorted(` appear ANYWHERE in this file" stays green here.
    Per-function granularity must not.
    """
    synthetic = '''
from secagent.affordances.priority import path_rank


def parse_findings(items, cfg):
    """Ranks and caps findings by severity — untouched, still correct."""
    out = list(items)
    out.sort(key=lambda i: i.severity)
    return out[:cfg.max_findings_per_file]


def select_targets(paths, cfg):
    """The regression: the ranking line was deleted here, the cap was not."""
    eligible = list(paths)
    return eligible[:cfg.max_files]
'''
    offenders = _segment_offenders("synthetic_probe_a.py", synthetic)
    assert offenders == {"synthetic_probe_a.py::select_targets": ["max_files"]}


def test_guard_is_silent_once_the_removed_sort_is_restored():
    """Silence pairing for the Probe A reproduction above: put the ranking back, and
    the same function must stop being flagged."""
    synthetic = '''
from secagent.affordances.priority import path_rank


def select_targets(paths, cfg):
    eligible = list(paths)
    eligible.sort(key=path_rank)
    return eligible[:cfg.max_files]
'''
    assert _segment_offenders("synthetic_ok.py", synthetic) == {}


def test_guard_catches_a_getattr_string_keyed_setting_reference():
    """Part 3(a): `getattr(cfg.scan, "max_files", 0)` spends the same budget as
    `cfg.scan.max_files`, but the setting name lives only inside a string literal —
    which `_code_only` strips before the reference check runs, by design. Without the
    separate, narrow `getattr(..., "max_*")` scan, the guard is silent on exactly the
    pattern `_code_only` was built to ignore everywhere else.
    """
    synthetic = '''
def select_targets(paths, cfg):
    cap = getattr(cfg.scan, "max_files", 0)
    return paths[:cap]
'''
    offenders = _segment_offenders("synthetic_getattr.py", synthetic)
    assert offenders == {"synthetic_getattr.py::select_targets": ["max_files"]}


def test_guard_is_silent_for_a_ranked_getattr_reference():
    """Silence pairing: the same `getattr` shape, but ranked, must not be flagged."""
    synthetic = '''
from secagent.affordances.priority import path_rank


def select_targets(paths, cfg):
    cap = getattr(cfg.scan, "max_files", 0)
    paths = sorted(paths, key=path_rank)
    return paths[:cap]
'''
    assert _segment_offenders("synthetic_getattr_ranked.py", synthetic) == {}


def test_code_only_strips_fstring_text_so_a_logged_setting_name_is_not_a_reference():
    """Part 3(b): a setting name that appears only inside an f-string's literal text
    (e.g. a log/skip-reason message like `f"skipped by scan.max_files={n} units"`)
    must not count as a code reference, the same way it wouldn't in an ordinary string
    or a comment.

    UNEXERCISED ON THIS INTERPRETER. The gate runs Python 3.11, where an f-string's
    literal text is still a single STRING token (no FSTRING_MIDDLE) — `_code_only`
    already stripped it before this fix, so this assertion passes with or without the
    fix here and proves nothing on 3.11. The leak this fix closes was verified
    directly on Python 3.13.1 with a standalone script comparing the pre-fix and
    post-fix tokenizer skip-lists over this exact source string (pre-fix: leaks
    `max_files`; post-fix: does not) — that script, not this test, is the real
    evidence, reported alongside this PR.
    """
    source = 'msg = f"skipped by scan.max_files={n} translation units"\n'
    assert "max_files" not in _code_only(source)


def test_a_per_file_finding_cap_keeps_the_most_severe():
    """The cap must not decide severity by accident — asked of `parse_findings` itself.

    This test used to build two `ScanFinding`s, call `findings.sort(key=...)` *in the
    test*, and assert the sort had worked. It never called `parse_findings`, so it
    proved that `SEVERITY_RANK` orders `critical` before `info` and that Python's
    `list.sort` works; deleting the sort from `parse_findings` could not make it fail.
    A test that cannot fail is worse than no test, because it manufactures confidence
    in the exact place the confidence is unearned.

    The real mechanism: the model emits findings in whatever order it likes, and the
    per-file cap keeps the FIRST `max_findings_per_file` of them. Without the sort a
    file with more findings than the cap drops a critical to keep an info — the cap
    silently deciding severity.
    """
    from dataclasses import replace

    from secagent.agents.scan.agent import parse_findings
    from secagent.agents.scan.rules import load_rules

    ruleset = replace(load_rules(RULES), max_findings_per_file=2)
    # Emitted worst-last, so keeping "the first two" without ranking keeps the two
    # least severe and discards the critical.
    payload = json.dumps([
        {"rule": "BUF-001", "line": 1, "severity": "info", "message": "i"},
        {"rule": "BUF-001", "line": 2, "severity": "low", "message": "l"},
        {"rule": "BUF-001", "line": 3, "severity": "medium", "message": "m"},
        {"rule": "BUF-001", "line": 4, "severity": "critical", "message": "c"},
    ])

    kept = parse_findings(payload, "a.c", ruleset)

    assert len(kept) == 2, "the cap must still bind — otherwise this proves nothing"
    assert [f.severity for f in kept] == ["critical", "medium"], (
        "the cap kept the findings the model happened to emit first instead of the "
        "most severe ones"
    )


# --- the four commands, behaviourally ----------------------------------------

def test_scan_selects_library_files_before_examples_and_vendored(tmp_path):
    """`_select_files`, called for real — not `sorted(..., key=path_rank)` in the test.

    Deleting `eligible.sort(key=path_rank)` from `_select_files` on the pre-guard code
    caused ZERO failures across the whole suite: the module-granular guard was vouched
    for by an unrelated `out.sort(...)` in `parse_findings`, and nothing else looked.
    The guard now catches it, but a text heuristic proving the word "sort" appears is
    not the same claim as the selection actually being right.

    Fixture parity: the library file sorts LAST alphabetically, so a selection that
    still secretly follows path order picks a demo or vendored file and fails here.
    """
    from secagent.agents.scan.agent import _select_files
    from secagent.config import Settings

    repo = tmp_path / "repo"
    (repo / "examples").mkdir(parents=True)
    (repo / "vendor").mkdir(parents=True)
    (repo / "zsrc").mkdir(parents=True)
    (repo / "examples" / "a_demo.c").write_text("int demo(void) { return 0; }\n")
    (repo / "vendor" / "b_third_party.c").write_text("int vend(void) { return 0; }\n")
    (repo / "zsrc" / "z_real.c").write_text("int real(void) { return 0; }\n")

    s = Settings()
    s.scan.max_files = 1
    targets, eligible = _select_files(repo, s, None, {"C", "C++"})

    assert len(eligible) == 3, "the fixture must offer a real choice"
    assert targets == ["zsrc/z_real.c"], (
        "the one file the budget bought must be the library file, even though it sorts "
        "last in path order"
    )


def test_testgen_generates_for_library_files_before_examples(tmp_path):
    """`generate_tests` run for real with the unit budget binding.

    `tests/test_config_and_testgen_counts.py` claimed to cover this with
    `sorted(["examples/demo.py", "src/click/core.py"], key=path_rank)` — a test of
    `path_rank`, not of testgen's use of it.
    """
    import httpx

    from secagent.agents.testgen.agent import generate_tests
    from secagent.config import Settings

    from .conftest import make_chat_response, mock_client

    repo = tmp_path / "repo"
    (repo / "examples").mkdir(parents=True)
    (repo / "vendor").mkdir(parents=True)
    (repo / "zsrc").mkdir(parents=True)
    (repo / "examples" / "a_demo.py").write_text("def demo():\n    return 1\n")
    (repo / "vendor" / "b_third.py").write_text("def vend():\n    return 2\n")
    (repo / "zsrc" / "z_real.py").write_text("def real():\n    return 3\n")

    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_unit_files = 1
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out",
                            functional=False, llm=llm)

    unit = result["coverage"]["unit_files"]
    assert unit["eligible"] == 3 and unit["attempted"] == 1, \
        "the budget must actually bind — otherwise this passes vacuously"
    assert [g["target"] for g in result["generated"]] == ["zsrc/z_real.py"], (
        "the one file the budget bought must be the library file, not the demo that "
        "sorts first"
    )



def test_triage_order_puts_own_code_first():
    from secagent.agents.analysis.agent import _triage_order
    from secagent.agents.analysis.models import Finding

    def f(path, severity="high"):
        return Finding(checker="c", status="error", severity=severity,
                       message="m", file=path, line=1)

    own, vendored = f("fsw/src/fm_cmds.c"), f("cFS/osal/src/os-impl.c")
    assert sorted([vendored, own], key=_triage_order)[0] is own


def test_triage_still_ranks_severity_above_ownership():
    from secagent.agents.analysis.agent import _triage_order
    from secagent.agents.analysis.models import Finding

    def f(path, severity):
        return Finding(checker="c", status="error", severity=severity,
                       message="m", file=path, line=1)

    high_vendor = f("cFS/osal/a.c", "high")
    low_own = f("fsw/src/b.c", "low")
    assert sorted([low_own, high_vendor], key=_triage_order)[0] is high_vendor


# --- the fifth command, behaviourally ------------------------------------------

def _compile_db(tmp_path, repo: Path, names: list[str]) -> Path:
    """The same shape `iter_compile_units` actually reads, built the way
    `tests/test_coverage_blocks.py::_compile_db` does — not a hand-rolled shape the
    real reader never sees."""
    entries = [{"directory": str(repo), "file": n, "arguments": ["cc", "-c", n]}
              for n in names]
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps(entries), encoding="utf-8")
    return db


def _fake_run_ikos():
    """Stands in for the real `ikos` binary: writes an empty SARIF report for
    whatever TU it's asked to analyze. Same shape as
    `test_coverage_blocks.py::_fake_run_ikos`."""
    def run(target, *, report_path, **kwargs):
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({"runs": [{"results": []}]}),
                                     encoding="utf-8")
        return Path(report_path)
    return run


def test_analyze_scan_ranks_translation_units_before_capping(tmp_path, monkeypatch):
    """The fifth instance of the bug (see module docstring): `analyze_scan` capped
    `iter_compile_units(db)` in whatever order the compile database listed its
    entries — no ranking, same shape as the other four.

    Fixture parity: the library files (`fsw/src/*`) sort AFTER the vendored
    (`cFS/osal/*`) and demo (`examples/*`) ones both alphabetically AND in the
    compile database's own entry order — so a fix that secretly still relies on
    path/compile-db order, rather than `path_rank`'s vendor/demo classification,
    fails this test instead of accidentally passing it.
    """
    from secagent.agents.analysis import agent as analysis_agent
    from secagent.agents.analysis.agent import analyze_scan
    from secagent.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    names = [
        "cFS/osal/src/a.c", "cFS/osal/src/b.c",
        "examples/demo1.c",
        "fsw/src/real1.c", "fsw/src/real2.c",
    ]
    db = _compile_db(tmp_path, repo, names)
    monkeypatch.setattr(analysis_agent, "run_ikos", _fake_run_ikos())
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")

    result = analyze_scan(repo, s, compile_db=str(db), out_dir=tmp_path / "o",
                          max_files=2, llm=None)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["translation_units"]
    skipped = {k for k, v in domain["why"].items() if v["state"] == "skipped"}
    # The two library files must be the ones that ran; everything vendored/demo —
    # despite sorting first in both the compile db and plain alphabetical order —
    # must be in the cap-skipped tail.
    assert skipped == {"cFS/osal/src/a.c", "cFS/osal/src/b.c", "examples/demo1.c"}
    assert data["scan"]["tus_eligible"] == 5
    assert data["scan"]["tus_total"] == 2

    # Silence pairing lives in `test_coverage_blocks.py::
    # test_analyze_scan_tu_clean_complete_run_is_silent`: with `max_files=0`,
    # `all_units` is never sliced at all, so ranking changes iteration order but
    # cannot change which TUs are analyzed, and `why` stays empty.


def test_a_root_level_test_file_is_not_library_code():
    """`gps-parser-test.cpp` sits at the repo root with a hyphen, and evaded every guard:
    `is_demo` reads directory segments only and a root-level file has none, while
    `call_map.is_test_path`'s regex required an underscore. So it sorted alphabetically
    before every `src/...` path, was processed FIRST in the function-description budget,
    and spent 11 of the 120-function cap on `test_empty`, `test_garbage` and friends
    before a line of library code was described. `UnicoreParser`'s nine methods, at the
    end of the queue, got none — so the docs describe the tests OF `UnicoreParser` in
    detail and say nothing about `UnicoreParser`.

    The distinction being drawn is not "match filenames" — see the test above, which must
    keep passing. It is that a test-shaped name inside a source directory is library code
    the directory vouches for, while a test-shaped name at the repo root has no directory
    vouching for it at all.
    """
    assert is_demo("gps-parser-test.cpp")
    assert is_demo("run-tests.sh")
    assert path_rank("src/unicore.cpp") < path_rank("gps-parser-test.cpp")


def test_a_root_level_file_that_is_not_test_shaped_is_still_library_code():
    """Paired silence. Plenty of real libraries put their sources at the root; "lives at
    the root" must not become a demotion on its own."""
    assert not is_demo("parser.cpp")
    assert not is_demo("setup.py")
    assert not is_demo("main.c")
