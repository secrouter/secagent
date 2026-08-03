"""Every unit of work must land in exactly one state: succeeded, partial, failed, or
skipped — so that `eligible == succeeded + partial + failed + skipped` always holds.

Two real defects broke that invariant.

`secagent scan` wrote a truncation note into `partial` at read time and never removed it
when the same file's rule groups later ALL failed — so one file counted as both
`files_partial` AND `files_failed` for a single eligible file, and a file whose rule
groups all failed and was ALSO truncated then had its truncation reason silently
overwritten by the rule-group-failure reason when it was only partly failed, discarding
half of the true story ("truncated" is a different fact from "some rule groups didn't
run", and a reader who only sees one of them can be sent to fix the wrong thing).

`secagent testgen` dropped a selected-but-unreadable file from `results` entirely by
returning `None` from `_gen_unit`'s inner `_one` — so it was not attempted, not failed,
not partial, not skipped. It simply vanished, and `attempted` under-reported by one.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.affordances.api import index_repo
from secagent.affordances.languages import is_code
from secagent.affordances.store import AffordanceStore
from secagent.agents.scan.agent import scan_repo
from secagent.agents.testgen.agent import generate_tests
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _scan_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def _testgen_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_unit_files = 5
    s.testgen.max_functional_components = 0
    return s


# --- scan: bug 1 — one file counted as both partial and failed ----------------

def test_a_truncated_and_wholly_failing_file_is_in_exactly_one_state(tmp_path):
    """A file that is BOTH truncated at read time AND has every rule group fail must
    count as `failed`, not as `failed` AND `partial`. `failed` dominates `partial`: a
    file whose rule groups all failed was never analysed at all, so the earlier
    truncation note is moot for counting (it must still survive in the failure text).

    A sharper way to see the old bug: `files_analyzed` is `scanned - len(failures)` — 0
    here — yet the old code also set `files_partial = 1`, so one of the "analysed"
    files was partial. Partial can never exceed analysed. It did.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) {\n" + "  int x = 0;\n" * 20 + "}\n")

    # Every call returns empty content, so every rule group fails outright.
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    s = _scan_settings(tmp_path)
    s.scan.max_file_bytes = 40          # small enough to truncate a.c
    s.scan.split_rules_by_category = True
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert result["files_failed"] == 1
    assert result["summary"]["files_partial"] == 0

    data = json.loads(Path(result["report_json"]).read_text())
    assert "a.c" in data["failures"]
    assert "a.c" not in data["partial"], \
        "a wholly-failed file must not also appear as partial"

    summary = data["summary"]
    files_analyzed = summary["files_analyzed"]
    files_partial = summary["files_partial"]
    files_failed = summary["files_failed"]
    files_skipped = summary["files_skipped_by_cap"]
    assert files_analyzed >= files_partial, \
        "partial can never exceed analysed — a partial file IS an analysed file"
    assert summary["files_eligible"] == (
        (files_analyzed - files_partial) + files_partial + files_failed + files_skipped
    )


def test_a_truncated_and_partly_failing_file_names_both_reasons(tmp_path):
    """A file that is truncated AND has SOME (not all) rule groups fail is correctly
    `partial` — but the report must name both causes. The old code overwrote the
    read-time truncation note with the rule-group-gaps message, discarding the
    truncation half. This repo has been burned before by a WRONG reason sending someone
    to fix a setting (an exclusion list) that had excluded nothing; the fix here is the
    same shape — never let one true reason silently replace another.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) {\n" + "  int x = 0;\n" * 20 + "}\n")

    def partial_llm(request):
        # The 'buffer' rule group fails; every other group succeeds.
        if b"BUF-001" in request.content:
            return httpx.Response(200, json=make_chat_response(content=""))
        return httpx.Response(200, json=make_chat_response(content="[]"))

    s = _scan_settings(tmp_path)
    s.scan.max_file_bytes = 40          # small enough to truncate a.c
    s.scan.split_rules_by_category = True
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=mock_client(partial_llm))

    data = json.loads(Path(result["report_json"]).read_text())
    assert "a.c" not in data["failures"]
    why = data["partial"]["a.c"]
    assert "never read" in why and "40" in why, "the truncation reason must survive"
    assert "rule group(s) were not applied" in why, "the rule-group-gap reason too"
    assert result["analysis_complete"] is False


# --- paired silence tests: must pass on old and new code alike ----------------

def test_a_truncated_but_fully_analysed_file_is_partial_not_failed(tmp_path):
    """A truncated file whose rule groups ALL succeed is `partial` (for the
    truncation) and must NOT appear in `failures`. Passes on old and new code — this
    is the case the old code got right by accident (it wrote straight into `partial`
    and there was no failure to collide with)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) {\n" + "  int x = 0;\n" * 20 + "}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    s.scan.max_file_bytes = 40
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert "a.c" in data["partial"]
    assert "never read" in data["partial"]["a.c"]
    assert "a.c" not in data["failures"]
    assert result["files_failed"] == 0


def test_a_complete_clean_uncapped_scan_reports_nothing_wrong(tmp_path):
    """The other half of the invariant: a genuinely complete, clean run must report
    zero partial, zero failed, and `analysis_complete is True`. Passes on old and new
    code — a run with nothing truncated and nothing failed never touches either bug."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    s.scan.max_files = 0
    s.scan.max_file_bytes = 40000
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert result["files_partial"] == 0
    assert result["files_failed"] == 0
    assert result["analysis_complete"] is True
    data = json.loads(Path(result["report_json"]).read_text())
    assert data["partial"] == {}
    assert data["failures"] == {}


# --- scan: attack the mechanism — cap + truncation + read failure together ----

def test_capped_truncated_and_unreadable_causes_are_named_separately(tmp_path):
    """A run that is simultaneously capped, truncated, AND has a read failure must name
    each cause on the right file, not collapse them. This exact combination previously
    produced a misattribution bug where an unreadable file was blamed on the file cap
    (`scan.max_files`) instead of on its own read failure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a_bad.c").write_bytes(b"\xff\xfe\x00 int main(void){}\n")   # not UTF-8
    (repo / "b_trunc.c").write_text("int main(void) {\n" + "  int x = 0;\n" * 20 + "}\n")
    for i in range(3):
        (repo / f"z_filler{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    def handler(request):
        if b"BUF-001" in request.content:
            return httpx.Response(200, json=make_chat_response(content=""))
        return httpx.Response(200, json=make_chat_response(content="[]"))

    s = _scan_settings(tmp_path)
    s.scan.max_files = 2                # only a_bad.c and b_trunc.c (path order) fit
    s.scan.max_file_bytes = 40          # truncates b_trunc.c
    s.scan.split_rules_by_category = True
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=mock_client(handler))

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_skipped_by_cap"] == 3, \
        "the 3 filler files were excluded by the cap"

    # The unreadable file: its own read failure, not the cap.
    assert "a_bad.c" in data["failures"]
    bad_reason = data["failures"]["a_bad.c"]
    assert "could not be read" in bad_reason
    assert "max_files" not in bad_reason and "cap" not in bad_reason

    # The truncated-and-partly-failed file: both of ITS causes, not the other file's.
    assert "b_trunc.c" in data["partial"]
    trunc_reason = data["partial"]["b_trunc.c"]
    assert "never read" in trunc_reason
    assert "rule group(s) were not applied" in trunc_reason
    assert "could not be read" not in trunc_reason, \
        "the unreadable file's reason must not bleed into this file's"

    assert "a_bad.c" not in data["partial"]
    assert "b_trunc.c" not in data["failures"]


# --- testgen: bug 3 — an unreadable target vanishes from results --------------

def _index_then_corrupt(tmp_path) -> tuple[Path, Settings]:
    """Index a repo with two valid, symbol-bearing files, THEN corrupt one on disk.

    Writing a non-UTF-8 `.py` file straight into the fixture does NOT reproduce the
    bug: the indexer (`affordances/api.py::_process_file`) also fails to read it, so it
    is stored with `n_symbols = 0` and `_gen_unit` filters it out via
    `is_code(r.language) and r.n_symbols > 0` before the bug ever has a chance to fire.
    The realistic reproduction — and a real user workflow — is: index while the file is
    still readable, THEN edit it. `queries.ensure_indexed` only builds an index when
    none exists yet, so the stale, still-eligible record from before the corruption is
    what `generate_tests` sees.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.py").write_text("def f():\n    return 1\n")
    (repo / "bad.py").write_text("def g():\n    return 2\n")   # valid at index time

    s = _testgen_settings(tmp_path)
    idx_report = index_repo(repo, s)
    assert idx_report["updated"] == 2

    (repo / "bad.py").write_bytes(b"\xff\xfe\x00")              # now unreadable

    # Fixture-parity proof, independent of generate_tests: the STORE's opinion of
    # bad.py, not generate_tests' behaviour, is what must show it is still eligible.
    # Asserting only "bad.py in result['generated']" would not prove this — on the old
    # code that file is dropped from `results` regardless of whether it was ever
    # selected, so a future indexer change that stopped selecting it could make that
    # assertion pass for the wrong reason.
    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        rec = next(r for r in store.file_records() if r.path == "bad.py")
    finally:
        store.close()
    assert is_code(rec.language) and rec.n_symbols > 0, (
        "the stale store record for bad.py must still look eligible "
        f"(language={rec.language!r}, n_symbols={rec.n_symbols}) — otherwise this test "
        "would not actually be exercising the unreadable-target path"
    )
    return repo, s


def test_an_unreadable_target_is_attempted_and_failed_not_dropped(tmp_path):
    """The file that went unreadable after indexing must still be counted: attempted
    and failed, with a reason — not silently missing from `results`."""
    repo, s = _index_then_corrupt(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    out = tmp_path / "out"
    result = generate_tests(repo, s, out_dir=out, functional=False, llm=llm)

    targets = {g["target"] for g in result["generated"]}
    assert "bad.py" in targets, \
        "fixture parity check: bad.py must still be selected as a target"

    assert result["attempted"] == 2
    assert result["failed"] == 1

    bad = next(g for g in result["generated"] if g["target"] == "bad.py")
    assert bad["ok"] is False
    assert bad["error"], "the failure must name a reason, not just ok=False"
    assert "read" in bad["error"].lower()

    manifest = json.loads((out / "manifest.json").read_text())
    manifest_targets = {t["target"] for t in manifest["tests"]}
    assert "bad.py" in manifest_targets, "manifest.json must still name the file"


def test_when_every_target_is_readable_nothing_is_reported_as_failed(tmp_path):
    """Paired silence test: with both files readable, `failed == 0` and no entry
    carries error text."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.py").write_text("def f():\n    return 1\n")
    (repo / "also_good.py").write_text("def g():\n    return 2\n")

    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    assert result["failed"] == 0
    assert all(not g["error"] for g in result["generated"])
