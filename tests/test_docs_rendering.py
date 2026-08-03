"""Docs rendering: RST-escaping of arbitrary model/source text, and the
function-description cap (truncation note + -1 = describe all)."""

from __future__ import annotations

from pathlib import Path

import httpx

from secagent.affordances.api import index_repo
from secagent.affordances.models import Component, FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore
from secagent.agents.docs.agent import _describe_functions
from secagent.agents.docs.outline import (
    _api_reference_page,
    _cap_counts_round_robin,
    _rst_escape,
    _rst_literal,
)
from secagent.config import Settings

from .conftest import make_chat_response, mock_client


def _llm(text: str = "does the thing."):
    return mock_client(
        lambda req: httpx.Response(200, json=make_chat_response(content=text)), model="m")


def test_rst_escape_neutralizes_inline_markup():
    assert _rst_escape("multiply 2 * x") == r"multiply 2 \* x"
    assert _rst_escape("uses `foo`, a|b, trailing_") == r"uses \`foo\`, a\|b, trailing\_"


def test_rst_literal_drops_backticks():
    assert _rst_literal("a`b") == "``a'b``"
    assert _rst_literal("int f(int *p)") == "``int f(int *p)``"


def test_api_reference_escapes_text_and_renders_note(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.upsert_file(
            FileRecord("a.c", "C", 1, "sha", 1, 1),
            FileSummary(path="a.c", purpose="does stuff"),
            [Symbol("f", "function", "a.c", 1, "int f(int *p)")],
        )
        store.set_symbol_doc("a.c", "f", "multiply by 2 * x; see `bar`")
        store.commit()
        comp = Component(name="(root)", path="(root)", kind="package")
        comp.files = ["a.c"]
        comp.language = "C"
        page = _api_reference_page(store, [comp], "5 of 9 functions described")
    finally:
        store.close()

    body = page.body
    assert ".. note::" in body and "5 of 9" in body      # truncation note rendered
    assert r"2 \* x" in body                              # stray emphasis escaped
    assert r"\`bar\`" in body                             # stray backticks escaped
    assert "``int f(int *p)``" in body                    # signature kept as literal


def _index(tmp_path, llm):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    s = Settings()
    s.affordances.store_dir = str(tmp_path / ".secagent")
    index_repo(tmp_path, s, llm=llm)
    return AffordanceStore(tmp_path, s.affordances.store_dir)


def test_describe_all_with_negative_cap(tmp_path):
    llm = _llm()
    store = _index(tmp_path, llm)
    try:
        described, total = _describe_functions(tmp_path, store, llm, -1)
        assert total == 2 and described == 2     # -1 describes every function
    finally:
        store.close()
        llm.close()


def test_cap_reports_truncation(tmp_path):
    llm = _llm()
    store = _index(tmp_path, llm)
    try:
        described, total = _describe_functions(tmp_path, store, llm, 1)
        assert total == 2 and described == 1     # capped at 1 of 2 → caller flags it
    finally:
        store.close()
        llm.close()


# --- the API reference row cap: round-robin across files, not file order --------------
#
# The defect: `_api_reference_page` used to walk `c.files` in order and lay each file's
# symbols end to end, then slice at `_MAX_API_ROWS`. Because the rows were laid end to
# end, the cap fell in the middle of ONE file and every file after it in `c.files`
# contributed nothing at all — measured directly against the real Click repository:
# 612 symbols, cap 300, 9 modules (including `utils.py` and `testing.py` — `echo` and
# `CliRunner`, the two symbols a Click user looks for first) wholly dropped. `path_rank`
# (see `affordances/priority.py`) does not fix this: every dropped file was equally-
# ranked library code in the same component, so ranking by vendored/demo/library would
# reorder nothing.

_CLICK_FIXTURES = Path(__file__).parent / "fixtures" / "click"


def _index_click_fixture(tmp_path: Path) -> AffordanceStore:
    """Copy the vendored click fixture (see fixtures/click/README.md) into `tmp_path`
    and index it for real, so `echo`/`CliRunner` come from the actual Python symbol
    extractor run over real Click source — not a hand-typed `Symbol`."""
    for name in ("_blocker.py", "utils.py", "testing.py"):
        (tmp_path / name).write_text(
            (_CLICK_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / ".secagent")
    index_repo(tmp_path, s, llm=None)
    return AffordanceStore(tmp_path, s.affordances.store_dir)


def test_api_reference_round_robins_symbol_budget_across_files(tmp_path):
    """Real Click source, real production cap (300, the `_api_reference_page` default —
    no cap injection needed here).

    `_blocker.py` (300 synthetic stub functions, sized to exactly match
    `_MAX_API_ROWS`; NOT Click source — see fixtures/click/README.md) is placed first
    in the component's file list, standing in for "whichever module happens to be
    large" (in the real repository that was `core.py` and everything alphabetically
    before it). Under the pre-fix file-order selection, `_blocker.py` alone exhausts
    the page's row budget, so `utils.py` and `testing.py` — and therefore `echo` and
    `CliRunner` — are wholly absent, with nothing in a row COUNT to reveal it: the page
    still reports exactly 300 kept, just the wrong 300.
    """
    store = _index_click_fixture(tmp_path)
    try:
        comp = Component(name="click", path="click", kind="package")
        comp.files = ["_blocker.py", "utils.py", "testing.py"]
        comp.language = "Python"
        page = _api_reference_page(store, [comp])
    finally:
        store.close()

    body = page.body
    # The named symbols a Click user looks for first — not just a count.
    assert "def echo(" in body, "echo (utils.py) must survive the page cap"
    assert "class CliRunner" in body, "CliRunner (testing.py) must survive the page cap"
    # The cap must actually bind — otherwise this proves nothing. Total real symbols
    # here: 300 (blocker) + 41 (utils.py) + 41 (testing.py) = 382; cap 300 kept, 82 cut
    # — all 82 from the blocker, since utils.py/testing.py are small enough to survive
    # a fair round-robin split in full. (utils.py counts 41, not the 34 this test was
    # written with, because the reference now also lists `kind="export"` — the PEP 562
    # re-exports it used to drop. That is the point of the change, not a regression.)
    assert "* … and 82 more symbol(s) in this component" in body


def test_a_clipped_file_gives_up_its_methods_before_its_public_types(tmp_path):
    """Round-robin alone was not enough, and the small fixture above hides why.

    With only three files the budget is generous enough that `utils.py` and
    `testing.py` survive whole, so `CliRunner` is never actually at risk there. On the
    real Click repository the `click` component has ~20 files sharing 300 rows — about
    15 each — and `testing.py` lists `CliRunner` 34th, behind the methods of five
    earlier classes. Measured after the round-robin fix, real Click regained `echo` and
    eight of its nine missing modules and STILL dropped `CliRunner`.

    So this drives the budget low enough that a file is genuinely clipped, which is the
    condition the production repo meets and the fixture otherwise does not. A reference
    page is read to find a type or a top-level function; members of types fill what is
    left.
    """
    store = _index_click_fixture(tmp_path)
    try:
        comp = Component(name="click", path="click", kind="package")
        comp.files = ["utils.py", "testing.py"]
        comp.language = "Python"
        page = _api_reference_page(store, [comp], max_rows=20)
    finally:
        store.close()

    body = page.body
    assert "* … and " in body, "the cap must actually clip, or this proves nothing"
    assert "class CliRunner" in body, (
        "a clipped file must give up methods before a public class — CliRunner is 34th "
        "in testing.py and cannot survive a 10-row share by declaration order"
    )
    assert "def echo(" in body, "echo is a top-level function and must survive too"


def test_api_reference_shares_budget_fairly_and_skips_empty_files(tmp_path):
    """Attack the mechanism directly (synthetic, not vendored — this is a property of
    the selection algorithm, not something that needs real source to demonstrate):

    - one file with many symbols sitting next to nine files with two symbols each —
      the round-robin split must not let the big file's early lead starve the small
      ones, the same shape as the Click fixture above but with the file-count skewed
      the other way;
    - one file with ZERO eligible symbols (e.g. a file that only declares constants) —
      it must be skipped outright: no empty section, no wasted round-robin turn, no
      effect on any other file's share.

    The cap (50) is injected via `max_rows` rather than driven by real source volume,
    per the fixture-parity note in the task: vendoring enough real source to make a
    300-row cap bind on nine-small-plus-one-big-file shape is impractical, so this
    drives the same real selection code (`_api_reference_page`, not a reimplementation
    of it) with a smaller cap instead.
    """
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.upsert_file(
            FileRecord("empty.py", "Python", 1, "sha-empty", 1, 0),
            FileSummary(path="empty.py"), [])
        store.upsert_file(
            FileRecord("big.py", "Python", 1, "sha-big", 1, 100),
            FileSummary(path="big.py"),
            [Symbol(f"big_{i}", "function", "big.py", i + 1, f"def big_{i}()")
             for i in range(100)])
        small_files = [f"small{i}.py" for i in range(9)]
        for name in small_files:
            store.upsert_file(
                FileRecord(name, "Python", 1, f"sha-{name}", 1, 2),
                FileSummary(path=name),
                [Symbol(f"{name}_a", "function", name, 1, f"def {name}_a()"),
                 Symbol(f"{name}_b", "function", name, 2, f"def {name}_b()")])
        store.commit()

        comp = Component(name="(root)", path="(root)", kind="package")
        comp.files = ["empty.py", "big.py", *small_files]
        comp.language = "Python"
        page = _api_reference_page(store, [comp], max_rows=50)
    finally:
        store.close()

    body = page.body
    # Every small file's BOTH symbols survive — none of the nine is starved just
    # because it sorts after the big file.
    for name in small_files:
        assert f"def {name}_a()" in body, f"{name}: first symbol must survive"
        assert f"def {name}_b()" in body, f"{name}: second symbol must survive"
    # The big file absorbs whatever the small files didn't need: 50 - 18 = 32.
    assert "def big_0()" in body
    assert "def big_31()" in body
    assert "def big_32()" not in body
    assert "* … and 68 more symbol(s) in this component" in body   # 118 total - 50 kept
    # The empty file rendered nothing and did not error.
    assert "empty.py" not in body


def test_api_reference_under_cap_lists_everything_and_is_silent(tmp_path):
    """Silence pairing for the two tests above: a component whose symbols fit under
    the cap must show every symbol and never print the truncation note — round-robin
    selection must be a no-op when the budget was never going to bind.

    Unlike the two tests above, this one passes against the pre-fix code too: the
    pre-fix bug was specifically about the cap BINDING (rows laid end to end, THEN
    sliced) — when nothing needs to be cut, file-order concatenation and round-robin
    selection produce the same output. It is included anyway because it is the
    explicit negative control the task asked for, and because it pins down that the
    round-robin rewrite does not turn a case that used to render cleanly into one that
    prints a spurious "and N more".
    """
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.upsert_file(
            FileRecord("a.py", "Python", 1, "sha-a", 1, 2),
            FileSummary(path="a.py"),
            [Symbol("f1", "function", "a.py", 1, "def f1()"),
             Symbol("f2", "function", "a.py", 2, "def f2()")])
        store.upsert_file(
            FileRecord("b.py", "Python", 1, "sha-b", 1, 2),
            FileSummary(path="b.py"),
            [Symbol("g1", "function", "b.py", 1, "def g1()"),
             Symbol("g2", "function", "b.py", 2, "def g2()")])
        store.commit()

        comp = Component(name="(root)", path="(root)", kind="package")
        comp.files = ["a.py", "b.py"]
        comp.language = "Python"
        page = _api_reference_page(store, [comp])
    finally:
        store.close()

    body = page.body
    assert "more symbol(s)" not in body
    for name in ("f1", "f2", "g1", "g2"):
        assert f"def {name}()" in body


def test_cap_counts_round_robin_more_files_than_cap_favors_earlier_files():
    """`_cap_counts_round_robin` directly: when the cap can't reach even one symbol
    per file, round-robin cannot do better than give the earliest files in the list a
    symbol each — there is no way to award a fractional symbol to the rest. This is a
    documented, deliberate limit (see the function's docstring), not a bug: it only
    bites when a component has more files than the row cap, a much narrower failure
    mode than the one this rewrite fixes (the cap binding inside a single file).

    This test necessarily fails against the pre-fix code — `_cap_counts_round_robin`
    does not exist there; the selection was a flat slice with no per-file notion at
    all.
    """
    counts = _cap_counts_round_robin([5, 5, 5, 5, 5], 3)
    assert counts == [1, 1, 1, 0, 0]
    assert sum(counts) == 3
