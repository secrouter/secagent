"""The `Provenance` retrofit into `scan`, `testgen`, and `analyze`.

Two evaluation agents each burned an entire run unable to tell whether the output in
front of them reflected a working model, a broken endpoint, or secagent's heuristic
fallback quietly taking over. `output.py` already guarantees a `Provenance` block
cannot be internally inconsistent (see `tests/test_output_model.py`); these tests are
about the other half — whether the three commands actually FEED it values that
reflect what really happened, rather than one plausible-looking guess. That is the
same fabrication failure mode `Coverage` exists to prevent, one field over.

Like `test_coverage_blocks.py`, the load-bearing check here is `_reconstruct`: it
rebuilds a `Provenance` from the JSON a command ACTUALLY wrote to disk and lets
`Provenance.__post_init__` re-validate it. If a legitimate run's own output cannot
pass that re-validation, the bug is in the command, not in the test.

Two required cases get the most attention, per the retrofit brief:

- `analyze` ingesting an IKOS report with triage off must report `model == []` AND
  `heuristic_only is False` — no model contributed, but IKOS (a real static
  analyzer) did, so this is NOT the heuristic-fallback case. Reporting
  `heuristic_only=True` here would be exactly the fabrication this module exists to
  prevent, just inverted: not "guessing a value", but "guessing a CAUSE".
- The two-model case (an index-time summary model differing from the current run's
  own model) is fixture-parity sensitive: `summary_model` MUST come from a real
  `secagent.affordances.api.index_repo` pass with a mocked transport, never a
  hand-set `store._set_meta(...)` call, or the test would not prove the real
  indexing path actually records what `Provenance` reads back out.

`scan` and `testgen` have no "no model at all" case: both commands always hold an
`LLMClient` in hand for their own per-file calls (there is no heuristic-fallback path
in either), so `model` is never empty for them. Only `analyze` can run with no model
contributing at all (IKOS-only, triage off) — that is the required case above.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from secagent.affordances.api import index_repo
from secagent.agents.analysis import agent as analysis_agent
from secagent.agents.analysis.agent import analyze_repo
from secagent.agents.scan import agent as scan_agent
from secagent.agents.scan.agent import scan_repo
from secagent.agents.testgen import agent as testgen_agent
from secagent.agents.testgen.agent import generate_tests
from secagent.config import Settings
from secagent.output import Provenance

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _reconstruct(block: dict) -> Provenance:
    """Rebuild a `Provenance` from a block pulled out of a REAL JSON artifact — never
    from a hand-built `Provenance` we already believe is consistent. Mirrors
    `test_coverage_blocks._reconstruct`: if a legitimate run's own on-disk output
    cannot satisfy `Provenance.__post_init__`, that is a bug in the command that
    wrote it, not in this test or in `output.py`.
    """
    return Provenance(
        secagent_version=block["secagent_version"], model=list(block["model"]),
        endpoint=block["endpoint"], generated_at=block["generated_at"],
        heuristic_only=block["heuristic_only"])


def _assert_realistic_timestamp_and_version(block: dict) -> None:
    """`generated_at` must be a real, parseable UTC timestamp close to "now", and
    `secagent_version` must be non-empty — i.e. `Provenance.now()` pulled a real value
    from `secagent.__version__` rather than leaving a placeholder."""
    assert block["secagent_version"]
    parsed = datetime.fromisoformat(block["generated_at"][:-1] + "+00:00")
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 300


class _BoomProvenance:
    """Stands in for `Provenance` to force construction to raise, simulating a future
    regression of an invariant `Provenance.__post_init__` would catch. Used to prove
    the three commands degrade gracefully — omitting `provenance` — rather than
    losing a whole run's output, or shipping a wrong/placeholder block, over an
    internal accounting bug. Mirrors `test_coverage_blocks._BoomCoverage`."""

    @classmethod
    def now(cls, *a, **k):
        raise ValueError("forced inconsistency for testing")


class _BoomCoverage:
    """Stands in for `Coverage` (mirrors `test_coverage_blocks._BoomCoverage`), used
    here only to attack the guarding mechanism from the OTHER direction — proving a
    forced COVERAGE failure leaves `provenance` untouched, the complement of the
    guarded-failure tests above which force a PROVENANCE failure and check
    `coverage` is untouched."""

    def __init__(self, *a, **k):
        raise ValueError("forced inconsistency for testing")


# ============================================================================
# scan
# ============================================================================

def _scan_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def test_scan_with_model_names_it_and_provenance_reconstructs(tmp_path):
    """The basic case: scan's own LLM client model must be named in `provenance.model`
    in BOTH the JSON artifact and the returned (stdout) dict, with `heuristic_only`
    False and `endpoint` the client's actual configured base_url — not
    `settings.llm.base_url`, so an injected client is reported truthfully."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(
        lambda r: httpx.Response(200, json=make_chat_response(content="[]")),
        model="scan-model")
    s = _scan_settings(tmp_path)
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    prov = _reconstruct(data["provenance"])
    assert prov.model == ["scan-model"]
    assert prov.heuristic_only is False
    assert prov.endpoint == "http://mock/v1"
    _assert_realistic_timestamp_and_version(data["provenance"])

    # Unlike coverage's `why`, provenance is five small keys — present in stdout too.
    ret_prov = _reconstruct(result["provenance"])
    assert ret_prov.model == ["scan-model"]


def test_scan_two_model_case_names_both_index_and_run_models(tmp_path):
    """Fixture parity: `summary_model` must come from a REAL `index_repo` pass under
    a mocked transport (never a hand-set `store._set_meta` call), so this proves the
    real indexing path records what `Provenance` reads back — not just that
    `Provenance` can hold two names if handed them by hand.

    An LLM-sourced index-time summary (model A) plus a scan run under a different
    client (model B) must produce `model == ["B", "A"]`: the run's own client first,
    the summary's model second, in the order they're documented to appear.

    NOTE: `index_repo` records `summary_model` from `settings.llm.model` (see
    `affordances/api.py` ~line 194), NOT from the injected client's own
    `llm.config.model` — so `s.llm.model` must be set to "index-model" here for the
    recorded meta to actually be "index-model", independent of what the injected
    mock client's own config says.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    s = _scan_settings(tmp_path)
    s.affordances.llm_summaries = True
    s.llm.model = "index-model"
    index_llm = mock_client(
        lambda r: httpx.Response(
            200, json=make_chat_response(content="Implements the program entrypoint.")),
        model="index-model")
    index_repo(repo, s, llm=index_llm)

    run_llm = mock_client(
        lambda r: httpx.Response(200, json=make_chat_response(
            content='[{"rule":"BUF-001","line":1,"severity":"high","message":"x"}]')),
        model="run-model")
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=run_llm)

    data = json.loads(Path(result["report_json"]).read_text())
    prov = _reconstruct(data["provenance"])
    assert prov.model == ["run-model", "index-model"]
    assert result["provenance"]["model"] == ["run-model", "index-model"]


def test_scan_provenance_construction_failure_is_logged_and_the_rest_of_the_report_survives(
    tmp_path, monkeypatch, caplog):
    """A future regression of a `Provenance` invariant must not destroy an otherwise-
    successful scan's entire output. On failure: log loudly (distinctively, so this
    is not mistaken for the SEPARATE coverage guard), and OMIT `provenance` entirely
    — never a partial or placeholder block. `coverage` must still be present: the two
    guards are independent, in EITHER direction (see the paired mechanism test
    below)."""
    monkeypatch.setattr(scan_agent, "Provenance", _BoomProvenance)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='[{"rule":"BUF-001","line":1,"severity":"high","message":"x"}]')))
    s = _scan_settings(tmp_path)
    with caplog.at_level("ERROR"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert "provenance" not in result
    data = json.loads(Path(result["report_json"]).read_text())
    assert "provenance" not in data
    assert data["findings"] and result["summary"]["total"] == 1
    assert "coverage" in result and "coverage" in data
    assert "provenance accounting is inconsistent" in caplog.text
    assert "scan:" in caplog.text


def test_scan_provenance_construction_normally_logs_nothing(tmp_path, caplog):
    """Paired silence test for the defensive wrap: an ordinary run must never emit
    the provenance internal-bug log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _scan_settings(tmp_path)
    with caplog.at_level("ERROR"):
        scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    assert "provenance accounting is inconsistent" not in caplog.text


def test_scan_provenance_survives_a_forced_coverage_failure(tmp_path, monkeypatch, caplog):
    """Attacking the same guarding mechanism from the OTHER direction: forcing
    `Coverage` (not `Provenance`) to raise must still leave `provenance` intact in
    both the JSON artifact and the returned dict — proving the two guarded regions
    really are independent, not just that one survives the other's failure."""
    monkeypatch.setattr(scan_agent, "Coverage", _BoomCoverage)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")),
                      model="scan-model")
    s = _scan_settings(tmp_path)
    with caplog.at_level("ERROR"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert "coverage" not in result
    assert "provenance" in result
    assert result["provenance"]["model"] == ["scan-model"]
    data = json.loads(Path(result["report_json"]).read_text())
    assert "coverage" not in data and "provenance" in data
    assert "coverage accounting is inconsistent" in caplog.text


# ============================================================================
# testgen
# ============================================================================

def _testgen_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_functional_components = 0
    return s


def test_testgen_with_model_names_it_and_provenance_reconstructs(tmp_path):
    """The basic case, mirroring scan's: testgen's own client model is named, with
    `heuristic_only` False and `endpoint` taken from the client in hand."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")),
        model="testgen-model")
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    prov = _reconstruct(manifest["provenance"])
    assert prov.model == ["testgen-model"]
    assert prov.heuristic_only is False
    assert prov.endpoint == "http://mock/v1"
    _assert_realistic_timestamp_and_version(manifest["provenance"])
    assert result["provenance"]["model"] == ["testgen-model"]


def test_testgen_two_model_case_names_both_index_and_run_models(tmp_path):
    """Fixture parity, same discipline as scan's two-model test: `summary_model`
    must come from a real `index_repo` pass, not a hand-set meta key. `_ask_unit`
    is what actually consumes the summary (see `_gen_unit`), so this exercises the
    unit-generation path specifically. `s.llm.model` (not the injected client's own
    config) is what `index_repo` records as `summary_model` — see the note on
    scan's two-model test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    s.affordances.llm_summaries = True
    s.llm.model = "index-model"
    index_llm = mock_client(
        lambda r: httpx.Response(
            200, json=make_chat_response(content="Defines a single helper function.")),
        model="index-model")
    index_repo(repo, s, llm=index_llm)

    run_llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")),
        model="run-model")
    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=run_llm)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    prov = _reconstruct(manifest["provenance"])
    assert prov.model == ["run-model", "index-model"]
    assert result["provenance"]["model"] == ["run-model", "index-model"]


def test_testgen_functional_two_model_case_names_both_models(tmp_path):
    """Attacking the same root cause on the OTHER summary-consuming path:
    `_gen_functional` reads `summaries[f].purpose` for each component member — a
    separate code path from `_gen_unit`'s `_ask_unit` call, and one that could
    easily have been missed by a fix that only patched the unit path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg_a").mkdir()
    (repo / "pkg_a" / "x.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    s.affordances.llm_summaries = True
    s.llm.model = "index-model"
    s.testgen.max_functional_components = 5
    index_llm = mock_client(
        lambda r: httpx.Response(
            200, json=make_chat_response(content="Implements package a's helper.")),
        model="index-model")
    index_repo(repo, s, llm=index_llm)

    run_llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")),
        model="run-model")
    result = generate_tests(repo, s, out_dir=tmp_path / "out", unit=False,
                            functional=True, llm=run_llm)

    assert result["provenance"]["model"] == ["run-model", "index-model"]


def test_testgen_provenance_construction_failure_is_logged_and_manifest_survives(
    tmp_path, monkeypatch, caplog):
    """Same defensive discipline as scan: a `Provenance` failure must not destroy the
    manifest or the generated tests, must log distinctively, and must leave
    `coverage` untouched."""
    monkeypatch.setattr(testgen_agent, "Provenance", _BoomProvenance)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    with caplog.at_level("ERROR"):
        result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)

    assert "provenance" not in result
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "provenance" not in manifest
    assert manifest["tests"], "the generated tests must still be written"
    assert "coverage" in manifest, "the coverage guard is separate and must be unaffected"
    assert "provenance accounting is inconsistent" in caplog.text


def test_testgen_provenance_construction_normally_logs_nothing(tmp_path, caplog):
    """Paired silence test: an ordinary testgen run must never emit the provenance
    internal-bug log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    s = _testgen_settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    with caplog.at_level("ERROR"):
        generate_tests(repo, s, out_dir=tmp_path / "out", functional=False, llm=llm)
    assert "provenance accounting is inconsistent" not in caplog.text


# ============================================================================
# analyze
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


def test_analyze_ikos_no_triage_model_empty_and_heuristic_only_false(tmp_path):
    """THE required case most likely to be got wrong: an IKOS report ingested with
    triage off has no model in the list — `_finalize` never touches an `LLMClient`
    at all in this run — but the findings still came from IKOS, a real static
    analyzer, not from secagent's heuristic fallback (analyze has none). The honest
    encoding is `model == []` AND `heuristic_only is False` together; asserting only
    one of the two would miss the exact mistake the brief warns about (reporting
    `heuristic_only=True` here would falsely tell a reader the findings were
    guessed).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(3, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)

    data = json.loads(Path(result["report_json"]).read_text())
    prov = _reconstruct(data["provenance"])
    assert prov.model == []
    assert prov.heuristic_only is False
    assert result["provenance"]["model"] == []
    assert result["provenance"]["heuristic_only"] is False
    # `endpoint` must still be the CONFIGURED endpoint even though no call was made —
    # a broken endpoint is one of the three states a reader is trying to distinguish,
    # and an absent/blank endpoint here would hide that entirely.
    assert prov.endpoint == s.llm.base_url
    _assert_realistic_timestamp_and_version(data["provenance"])


def test_analyze_with_triage_names_the_triage_model(tmp_path):
    """When triage DOES run, the triage client's own model is named — taken from the
    client in hand, not `settings.llm.model`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 5
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Likely a false positive.")),
        model="triage-model")
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o",
                          run=False, llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    prov = _reconstruct(data["provenance"])
    assert prov.model == ["triage-model"]
    assert prov.heuristic_only is False
    assert prov.endpoint == "http://mock/v1"


def test_analyze_two_model_case_names_both_index_and_triage_models(tmp_path):
    """Fixture parity, same discipline as scan/testgen's two-model tests: index a
    real file with a mocked transport under one model, then triage findings that
    reference that exact file under a different model, and assert both survive into
    `provenance.model`, client model first. `s.llm.model` (not the injected client's
    own config) is what `index_repo` records as `summary_model` — see the note on
    scan's two-model test."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "f0.c").write_text("int f0(void) { return 0; }\n")
    report = _ikos_findings_report(1, tmp_path)
    s = _analysis_settings(tmp_path)
    s.affordances.llm_summaries = True
    s.llm.model = "index-model"
    index_llm = mock_client(
        lambda r: httpx.Response(
            200, json=make_chat_response(content="Implements helper f0.")),
        model="index-model")
    index_repo(repo, s, llm=index_llm)

    s.analysis.max_triage = 5
    triage_llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Likely a false positive.")),
        model="triage-model")
    result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o",
                          run=False, llm=triage_llm)

    data = json.loads(Path(result["report_json"]).read_text())
    prov = _reconstruct(data["provenance"])
    assert prov.model == ["triage-model", "index-model"]
    assert result["provenance"]["model"] == ["triage-model", "index-model"]


def test_analyze_provenance_construction_failure_is_logged_and_the_rest_of_the_report_survives(
    tmp_path, monkeypatch, caplog):
    """Same defensive discipline as scan/testgen, and same independence-from-coverage
    property: forcing `Provenance` to raise must not touch `coverage`, must log
    distinctively, and must leave findings/summary intact."""
    monkeypatch.setattr(analysis_agent, "Provenance", _BoomProvenance)
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    with caplog.at_level("ERROR"):
        result = analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)

    assert "provenance" not in result
    data = json.loads(Path(result["report_json"]).read_text())
    assert "provenance" not in data
    assert data["findings"] and result["summary"]["total"] == 2
    assert "coverage" in result and "coverage" in data
    assert "provenance accounting is inconsistent" in caplog.text


def test_analyze_provenance_construction_normally_logs_nothing(tmp_path, caplog):
    """Paired silence test: an ordinary analyze run must never emit the provenance
    internal-bug log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    report = _ikos_findings_report(2, tmp_path)
    s = _analysis_settings(tmp_path)
    s.analysis.max_triage = 0
    with caplog.at_level("ERROR"):
        analyze_repo(repo, s, ikos_report=report, out_dir=tmp_path / "o", run=False)
    assert "provenance accounting is inconsistent" not in caplog.text
