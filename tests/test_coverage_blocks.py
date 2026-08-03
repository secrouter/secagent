"""The `Coverage` retrofit into `scan`, `testgen`, and `analyze`.

`output.py` already guarantees a `Coverage` cannot be internally inconsistent — these
tests are about the other half: whether the three commands actually FEED it numbers
derived from the same data their legacy keys come from, rather than a second,
independently-computed guess. The risk this whole design exists to prevent is not "no
coverage block" — it is a *fake* one: plausible numbers nobody actually tallied, sitting
in a report next to numbers that were.

The single most load-bearing check in this file is not a hand-built `Coverage` — it is
`_reconstruct`, which rebuilds a `Coverage` from the JSON a command ACTUALLY wrote to
disk (via `scan.json` / `manifest.json` / `analysis.json`) and lets `Coverage.__post_init__`
re-validate it. If a legitimate run's own output cannot pass that re-validation, the bug
is in the command, not in the test.

Each disclosure test (bounded / failing) is paired with a silence test (clean, complete,
unbounded): a notice that always fires is one nobody reads, and `why` must be empty when
there is genuinely nothing to disclose.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.affordances.api import index_repo
from secagent.affordances.languages import is_code
from secagent.affordances.store import AffordanceStore
from secagent.agents.analysis import agent as analysis_agent
from secagent.agents.analysis.agent import analyze_repo, analyze_scan
from secagent.agents.scan import agent as scan_agent
from secagent.agents.scan.agent import scan_repo
from secagent.agents.testgen import agent as testgen_agent
from secagent.agents.testgen.agent import generate_tests
from secagent.config import Settings
from secagent.output import Coverage, UnitState

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _reconstruct(domain: dict) -> Coverage:
    """Rebuild a `Coverage` from a domain dict pulled out of a REAL JSON artifact —
    never from a hand-built `Coverage` we already believe is consistent. This is the
    check the brief calls out as the one that earns its keep: if a legitimate run's
    own on-disk output cannot satisfy `Coverage.__post_init__`, that is a bug in the
    command, and the fix is in the command, not in this test or in `output.py`.
    """
    why = {unit_id: UnitState(state=w["state"], reason=w["reason"])
          for unit_id, w in domain.get("why", {}).items()}
    return Coverage(eligible=domain["eligible"], attempted=domain["attempted"],
                    succeeded=domain["succeeded"], partial=domain["partial"],
                    failed=domain["failed"], skipped=domain["skipped"], why=why)


class _BoomCoverage:
    """Stands in for `Coverage` to force the constructor to raise, simulating a future
    regression of an invariant (e.g. state-exclusivity) that the real `Coverage` would
    catch. Used to prove the three commands degrade gracefully rather than losing an
    entire run's output over an internal accounting bug."""

    def __init__(self, *a, **k):
        raise ValueError("forced inconsistency for testing")


# ============================================================================
# scan — one domain: `files`
# ============================================================================

def _scan_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def test_scan_bounded_run_names_the_skipped_files_and_the_block_reconstructs(tmp_path):
    """A capped scan must name WHICH files were never looked at, not just how many —
    the improvement the brief calls for in `_select_files` (it used to discard the
    eligible list and return only a count). The block pulled back out of `scan.json`
    must itself satisfy `Coverage`'s invariant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    s.scan.max_files = 2
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["files"]
    cov = _reconstruct(domain)
    assert cov.eligible == 5 and cov.attempted == 2 and cov.skipped == 3
    assert cov.complete is False and domain["complete"] is False

    skipped_why = {k: v for k, v in domain["why"].items() if v["state"] == "skipped"}
    assert len(skipped_why) == 3
    assert all(v["reason"] == "scan.max_files=2" for v in skipped_why.values())

    # The returned (stdout) dict carries the same numbers but never `why` — a
    # 5000-file repo's per-file map would flood a model's context.
    ret_domain = result["coverage"]["files"]
    assert "why" not in ret_domain
    assert ret_domain["eligible"] == 5 and ret_domain["skipped"] == 3


def test_scan_failing_run_names_the_failed_files_and_the_block_reconstructs(tmp_path):
    """Every file failing (empty model output) must show up as `failed` in coverage
    with the SAME reason string as the legacy `failures` dict — never a second,
    independently-worded computation of the same fact."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    s = _scan_settings(tmp_path)
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["files"]
    cov = _reconstruct(domain)
    assert cov.failed == 3 and cov.succeeded == 0 and cov.skipped == 0
    assert cov.complete is False

    failed_why = {k: v for k, v in domain["why"].items() if v["state"] == "failed"}
    assert len(failed_why) == 3
    assert all("empty content" in v["reason"] for v in failed_why.values())
    for f, reason in data["failures"].items():
        assert domain["why"][f]["reason"] == reason


def test_scan_clean_complete_run_coverage_is_silent_and_reconstructs(tmp_path):
    """Paired silence test: an unbounded, fully successful scan must report
    `complete: true` with an EMPTY `why` — a notice that always fires is one nobody
    reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    s.scan.max_files = 0
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["files"]
    cov = _reconstruct(domain)
    assert cov.complete is True
    assert domain["complete"] is True
    assert domain["why"] == {}
    assert result["coverage"]["files"]["complete"] is True


def test_scan_explicit_paths_with_a_cap_names_the_right_reason(tmp_path):
    """`_select_files` has two branches (explicit `paths=` vs. the directory walk);
    the cap reason must be the real setting (`scan.max_files=N`) in both, not
    something branch-specific."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(4):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    s.scan.max_files = 2
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm,
                       paths=["m0.c", "m1.c", "m2.c", "m3.c"])

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["files"]
    _reconstruct(domain)
    skipped = {k: v["reason"] for k, v in domain["why"].items() if v["state"] == "skipped"}
    assert skipped == {"m2.c": "scan.max_files=2", "m3.c": "scan.max_files=2"}


def test_scan_coverage_construction_failure_is_logged_and_the_rest_of_the_report_survives(
    tmp_path, monkeypatch, caplog):
    """A future regression of the state-exclusivity invariant (fixed in the previous
    PR) must not destroy an otherwise-successful scan's ENTIRE output. On a
    `Coverage.__post_init__` failure: log loudly, and OMIT `coverage` — never a
    partial or zeroed block, which would be indistinguishable from a real one — while
    findings, summary, and every legacy key are still written normally."""
    monkeypatch.setattr(scan_agent, "Coverage", _BoomCoverage)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='[{"rule":"BUF-001","line":1,"severity":"high","message":"x"}]')))
    s = _scan_settings(tmp_path)
    with caplog.at_level("ERROR"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert "coverage" not in result
    data = json.loads(Path(result["report_json"]).read_text())
    assert "coverage" not in data
    assert data["findings"] and result["summary"]["total"] == 1
    assert "INTERNAL BUG" in caplog.text and "scan:" in caplog.text


def test_scan_coverage_construction_normally_logs_nothing(tmp_path, caplog):
    """Paired silence test for the defensive wrap: an ordinary run must never emit
    the internal-bug log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    with caplog.at_level("ERROR"):
        scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    assert "INTERNAL BUG" not in caplog.text


# ============================================================================
# testgen — two domains: `unit_files`, `components`
# ============================================================================

def _testgen_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _testgen_index_then_corrupt(tmp_path):
    """Index while readable, THEN corrupt on disk — see
    tests/test_coverage_state_exclusivity.py::_index_then_corrupt for why writing a
    non-UTF-8 file straight into the fixture does not reproduce the target path (the
    indexer would record `n_symbols=0` and `_gen_unit` filters it out before ever
    reaching it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.py").write_text("def f():\n    return 1\n")
    (repo / "bad.py").write_text("def g():\n    return 2\n")
    s = _testgen_settings(tmp_path)
    s.testgen.max_unit_files = 5
    s.testgen.max_functional_components = 0
    idx = index_repo(repo, s)
    assert idx["updated"] == 2

    (repo / "bad.py").write_bytes(b"\xff\xfe\x00")

    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        rec = next(r for r in store.file_records() if r.path == "bad.py")
    finally:
        store.close()
    assert is_code(rec.language) and rec.n_symbols > 0, (
        "fixture-parity check: the stale store record must still look eligible, or "
        "this test is not exercising the unreadable-target path at all")
    return repo, s


def test_testgen_unit_files_failing_run_reconstructs(tmp_path):
    """An unreadable target must count as `failed` in the `unit_files` domain, with
    the SAME `error` text as `generated`/`manifest.json` — not a second guess."""
    repo, s = _testgen_index_then_corrupt(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    domain = manifest["coverage"]["unit_files"]
    cov = _reconstruct(domain)
    assert cov.failed == 1 and cov.succeeded == 1 and cov.attempted == 2
    assert cov.complete is False

    bad = next(g for g in result["generated"] if g["target"] == "bad.py")
    bad_why = domain["why"]["bad.py"]
    assert bad_why["state"] == "failed"
    assert bad_why["reason"] == bad["error"]
    assert result["coverage"]["unit_files"]["failed"] == 1


def test_testgen_unit_files_bounded_run_names_the_skipped_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    s = _testgen_settings(tmp_path)
    s.testgen.max_unit_files = 2
    s.testgen.max_functional_components = 0
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    domain = manifest["coverage"]["unit_files"]
    cov = _reconstruct(domain)
    assert cov.eligible == 5 and cov.attempted == 2 and cov.skipped == 3
    assert cov.complete is False
    skipped_reasons = {v["reason"] for v in domain["why"].values() if v["state"] == "skipped"}
    assert skipped_reasons == {"testgen.max_unit_files=2"}


def test_testgen_unit_files_clean_complete_run_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    s = _testgen_settings(tmp_path)
    s.testgen.max_functional_components = 0
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    domain = manifest["coverage"]["unit_files"]
    cov = _reconstruct(domain)
    assert cov.complete is True
    assert domain["why"] == {}
    assert result["coverage"]["unit_files"]["complete"] is True


def test_testgen_components_domain_absent_when_functional_pass_did_not_run(tmp_path):
    """`components` must not be invented for a population that was never even looked
    at — the same named risk as fabricating `translation_units` for `analyze run`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    assert "components" not in result["coverage"]
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "components" not in manifest["coverage"]


def test_gen_functional_populates_error_on_not_ok_and_components_domain_reconstructs(
    tmp_path):
    """Known gap named in the brief: the previous PR populated `.error` on every
    not-ok path in `_gen_unit` but not `_gen_functional`, where `ok = _write(...)` is
    False whenever the model returned nothing usable — no reason was recorded. A
    `components` coverage domain whose `why` said "failed" with no cause is the same
    empty-answer failure the `error` field exists to prevent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg_a").mkdir()
    (repo / "pkg_a" / "x.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    result = generate_tests(repo, s, out_dir=tmp_path / "out", unit=False,
                            functional=True, llm=llm)

    comp = next(g for g in result["generated"] if g["kind"] == "functional")
    assert comp["ok"] is False
    assert comp["error"], "the failure must name a reason, not just ok=False"

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    domain = manifest["coverage"]["components"]
    cov = _reconstruct(domain)
    assert cov.failed == 1 and cov.complete is False
    assert domain["why"][comp["target"]]["reason"] == comp["error"]


def test_testgen_components_bounded_run_names_the_skipped_components(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("pkg_a", "pkg_b", "pkg_c"):
        d = repo / name
        d.mkdir()
        (d / "x.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    s.testgen.max_functional_components = 1
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    generate_tests(repo, s, out_dir=tmp_path / "out", unit=False, functional=True, llm=llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    domain = manifest["coverage"]["components"]
    cov = _reconstruct(domain)
    assert cov.eligible == 3 and cov.attempted == 1 and cov.skipped == 2
    assert cov.complete is False
    skipped_reasons = {v["reason"] for v in domain["why"].values() if v["state"] == "skipped"}
    assert skipped_reasons == {"testgen.max_functional_components=1"}


def test_testgen_coverage_construction_failure_is_logged_and_manifest_survives(
    tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(testgen_agent, "Coverage", _BoomCoverage)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    with caplog.at_level("ERROR"):
        result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    assert "coverage" not in result
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "coverage" not in manifest
    assert manifest["tests"], "the generated tests must still be written"
    assert "INTERNAL BUG" in caplog.text


def test_testgen_coverage_construction_normally_logs_nothing(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    with caplog.at_level("ERROR"):
        generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)
    assert "INTERNAL BUG" not in caplog.text


# ============================================================================
# analyze — `triage` (always), `translation_units` (analyze_scan only)
# ============================================================================

def _analysis_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _ikos_findings_report(n: int, tmp_path) -> Path:
    items = [{"check": "boa", "result": "error", "filename": f"src/f{i}.c",
             "lineno": i + 1, "msg": f"issue {i}"} for i in range(n)]
    p = tmp_path / "ikos.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return p


def test_analyze_triage_not_run_names_max_triage_as_the_cause(tmp_path):
    """`_finalize` used to set `triage_stats = {}` when triage never ran, and the
    report then said NOTHING — "no assessment" could mean either "nothing to add" or
    "every call failed", the exact defect the triage-health work was done to fix.
    `triage` must be present and honest even when triage did not run at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(3, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["triage"]
    cov = _reconstruct(domain)
    assert cov.eligible == 3 and cov.attempted == 0 and cov.skipped == 3
    assert cov.complete is False
    assert len(domain["why"]) == 3
    assert all(v["reason"] == "analysis.max_triage=0" for v in domain["why"].values())
    # `translation_units` must never be invented for ingest mode — the named risk of
    # this whole design.
    assert set(data["coverage"]) == {"triage"}
    assert set(result["coverage"]) == {"triage"}


def test_analyze_triage_not_run_names_no_model_configured(tmp_path):
    """The OTHER cause `_finalize` can hit: a nonzero budget but no model endpoint."""
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 5   # nonzero, but llm_summaries is False and no llm= given
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["triage"]
    _reconstruct(domain)
    assert all(v["reason"] == "no model endpoint configured" for v in domain["why"].values())


def test_analyze_triage_bounded_run_names_the_budget_skipped_findings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(5, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 2
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Likely a false positive.")))
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o",
                          run=False, llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["triage"]
    cov = _reconstruct(domain)
    assert cov.eligible == 5 and cov.attempted == 2 and cov.succeeded == 2
    assert cov.skipped == 3 and cov.complete is False
    skipped_reasons = {v["reason"] for v in domain["why"].values() if v["state"] == "skipped"}
    assert skipped_reasons == {"analysis.max_triage=2"}


def test_analyze_triage_failing_run_reconstructs_and_carries_every_failure(tmp_path):
    """`_triage` truncates its `failures` list to 10 for the legacy dict; coverage
    `why` must carry ALL of them (12 here), not the truncated 10."""
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(12, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 12
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o",
                          run=False, llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["triage"]
    cov = _reconstruct(domain)
    assert cov.attempted == 12 and cov.failed == 12 and cov.succeeded == 0
    assert cov.complete is False
    failed_why = {k: v for k, v in domain["why"].items() if v["state"] == "failed"}
    assert len(failed_why) == 12, (
        "why must carry every failure, not just the 10 kept for the legacy dict")
    assert len(data["summary"]["triage"]["failures"]) == 10, "the legacy field stays capped"


def test_analyze_triage_clean_complete_run_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(3, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 10
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Likely a false positive.")))
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o",
                          run=False, llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["triage"]
    cov = _reconstruct(domain)
    assert cov.complete is True
    assert domain["why"] == {}


def _compile_db(tmp_path, repo: Path, names: list[str]) -> Path:
    entries = [{"directory": str(repo), "file": n, "arguments": ["cc", "-c", n]}
              for n in names]
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps(entries), encoding="utf-8")
    return db


def _fake_run_ikos(fail_names: set[str]):
    """Stands in for the real `ikos` binary: writes an empty SARIF report (no
    findings, no error) for every TU not named in `fail_names`, and raises for the
    ones that are — the same `(RuntimeError, OSError)` shape `_scan_one` catches."""
    def run(target, *, report_path, **kwargs):
        name = Path(target).name
        if name in fail_names:
            raise RuntimeError(
                f"src/{name}:1:1: fatal error: 'missing.h' file not found\n"
                f"ikos: error while compiling {name}, abort.")
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({"runs": [{"results": []}]}),
                                     encoding="utf-8")
        return Path(report_path)
    return run


def test_analyze_scan_tu_bounded_run_reports_the_pre_cap_eligible_count(
    tmp_path, monkeypatch):
    """`analyze_scan` slices `units = units[:max_files]` BEFORE computing `tus_total`,
    so a naive `tus_skipped` over the post-cap count always reads 0 no matter how many
    TUs the cap actually excluded — "reported 0 skipped for files that were never
    considered". The `translation_units` domain, and the new `tus_eligible` legacy
    key, must report the PRE-cap population."""
    repo = tmp_path / "repo"
    repo.mkdir()
    names = [f"f{i}.c" for i in range(5)]
    db = _compile_db(tmp_path, repo, names)
    monkeypatch.setattr(analysis_agent, "run_ikos", _fake_run_ikos(set()))
    s = _analysis_settings(tmp_path)
    result = analyze_scan(repo, s, compile_db=str(db), out_dir=tmp_path / "o",
                          max_files=2, llm=None)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["scan"]["tus_total"] == 2       # legacy, POST-cap
    assert data["scan"]["tus_eligible"] == 5     # new, PRE-cap — the honest number
    assert data["scan"]["tus_skipped"] == 0      # legacy: IKOS failures, not cap-skips

    domain = data["coverage"]["translation_units"]
    cov = _reconstruct(domain)
    assert cov.eligible == 5 and cov.attempted == 2 and cov.skipped == 3
    assert cov.complete is False
    skipped_reasons = {v["reason"] for v in domain["why"].values() if v["state"] == "skipped"}
    assert skipped_reasons == {"--max-files=2"}


def test_analyze_scan_tu_failing_run_carries_the_diagnostic(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    names = [f"f{i}.c" for i in range(3)]
    db = _compile_db(tmp_path, repo, names)
    monkeypatch.setattr(analysis_agent, "run_ikos", _fake_run_ikos({"f1.c"}))
    s = _analysis_settings(tmp_path)
    result = analyze_scan(repo, s, compile_db=str(db), out_dir=tmp_path / "o",
                          max_files=0, llm=None)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["translation_units"]
    cov = _reconstruct(domain)
    assert cov.attempted == 3 and cov.failed == 1 and cov.succeeded == 2
    assert cov.complete is False
    failed = {k: v for k, v in domain["why"].items() if v["state"] == "failed"}
    assert len(failed) == 1
    assert "missing.h" in next(iter(failed.values()))["reason"]


def test_analyze_scan_tu_clean_complete_run_is_silent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    names = [f"f{i}.c" for i in range(3)]
    db = _compile_db(tmp_path, repo, names)
    monkeypatch.setattr(analysis_agent, "run_ikos", _fake_run_ikos(set()))
    s = _analysis_settings(tmp_path)
    result = analyze_scan(repo, s, compile_db=str(db), out_dir=tmp_path / "o",
                          max_files=0, llm=None)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["translation_units"]
    cov = _reconstruct(domain)
    assert cov.complete is True
    assert domain["why"] == {}


def test_analyze_coverage_construction_failure_is_logged_and_the_rest_of_the_report_survives(
    tmp_path, monkeypatch, caplog):
    """Same defensive wrap as scan/testgen, exercised here against BOTH domains
    `_finalize` can build (`translation_units` passed in as raw kwargs, `triage` built
    internally) — both are constructed in one guarded region, so a failure in either
    omits the whole `coverage` key rather than a half-built one."""
    monkeypatch.setattr(analysis_agent, "Coverage", _BoomCoverage)
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    with caplog.at_level("ERROR"):
        result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)

    assert "coverage" not in result
    data = json.loads(Path(result["report_json"]).read_text())
    assert "coverage" not in data
    assert data["findings"] and result["summary"]["total"] == 2
    assert "INTERNAL BUG" in caplog.text


def test_analyze_coverage_construction_normally_logs_nothing(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    with caplog.at_level("ERROR"):
        analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)
    assert "INTERNAL BUG" not in caplog.text
