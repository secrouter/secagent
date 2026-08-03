"""Tests for UC3: C/C++ static analysis via IKOS (ingest mode + enrichment)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.analysis.agent import analyze_repo
from secagent.agents.analysis.ikos import parse_ikos_report
from secagent.agents.analysis.models import severity_for
from secagent.agents.analysis.report import render_markdown, summarize
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

FIXTURE = Path(__file__).parent / "fixtures" / "cpp_repo"
REPORT = FIXTURE / "ikos-report.json"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.analysis.max_triage = 0  # no LLM by default
    return s


def test_severity_mapping():
    assert severity_for("error") == "high"
    assert severity_for("warning") == "medium"
    assert severity_for("ok") == "ok"
    assert severity_for("safe") == "ok"


def test_parser_filters_safe_and_reads_fields():
    findings = parse_ikos_report(REPORT.read_text())
    # The "ok" result is dropped; 3 actionable findings remain.
    assert len(findings) == 3
    boa = next(f for f in findings if f.function == "copy_into")
    assert boa.checker == "boa" and boa.severity == "high" and boa.line == 8
    assert "overflow" in boa.message


def test_parser_tolerates_top_level_list_and_alt_keys():
    data = [
        {"check": "dbz", "result": "error", "filename": "a.c", "lineno": 3, "msg": "x"},
        {"kind": "uva", "status": "warning", "location": {"file": "b.c", "line": 9}},
    ]
    findings = parse_ikos_report(json.dumps(data))
    assert len(findings) == 2
    assert findings[0].file == "a.c" and findings[0].line == 3
    assert findings[1].file == "b.c" and findings[1].line == 9


def test_summarize_counts():
    findings = parse_ikos_report(REPORT.read_text())
    s = summarize(findings)
    assert s["total"] == 3
    assert s["high"] == 1
    assert s["medium"] == 2
    assert s["by_checker"]["boa"] == 2


def test_render_markdown_includes_locations_and_marking():
    findings = parse_ikos_report(REPORT.read_text())
    md = render_markdown(findings, project="cpp_repo", summary=summarize(findings),
                         banner="CUI")
    assert md.startswith("**CUI**")
    assert "src/buf.c:8" in md
    assert "HIGH [boa]" in md


def test_analyze_ingest_end_to_end(tmp_path):
    s = _settings(tmp_path)
    out = tmp_path / "analysis"
    result = analyze_repo(FIXTURE, s, ikos_report=REPORT, out_dir=out, run=False)
    assert result["summary"]["total"] == 3
    assert result["summary"]["high"] == 1
    assert Path(result["report_md"]).exists()
    data = json.loads(Path(result["report_json"]).read_text())
    # Enrichment: findings carry the component derived from the affordance store.
    assert any(f["component"] == "src" for f in data["findings"])
    assert any("buffer copy" in (f["file_purpose"] or "").lower() for f in data["findings"])


def test_analyze_triage_with_mock_llm(tmp_path):
    s = _settings(tmp_path)
    s.analysis.max_triage = 5
    llm = mock_client(
        lambda r: httpx.Response(200, json=make_chat_response(
            content="Likely true positive: strcpy has no bound check."))
    )
    out = tmp_path / "analysis"
    result = analyze_repo(FIXTURE, s, ikos_report=REPORT, out_dir=out, run=False, llm=llm)
    data = json.loads(Path(result["report_json"]).read_text())
    assert any(f["triage"].get("assessment") for f in data["findings"])
    assert "true positive" in Path(result["report_md"]).read_text().lower()


def test_analyze_requires_a_source(tmp_path):
    s = _settings(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        analyze_repo(FIXTURE, s, out_dir=tmp_path / "o", run=False)


# --- 3.2: a TU IKOS refuses to analyse must degrade, never traceback ---------------

def _real_nm_output() -> str:
    """A real `llvm-nm --defined-only` dump of a C++ TU's bitcode.

    The address column is dashes for bitcode (there are no addresses yet), and the class
    letter is the second-to-last field — the shape the parser actually meets. `_ZN4RtcmC2Ev`
    is the mangled default constructor from the `rtcm.cpp` run that crashed: emitted weak
    by our compilation, and not necessarily by IKOS's.
    """
    return (
        "---------------- T _ZN4Rtcm6decodeEPKhm\n"
        "---------------- T _Z11rtcm_createv\n"
        "---------------- t _ZL15internal_helperv\n"
        "---------------- W _ZN4RtcmC2Ev\n"
        "---------------- W _ZN4RtcmD2Ev\n"
        "---------------- U _ZSt4cerr\n"          # undefined — not ours at all
        "---------------- D _ZN4Rtcm5tableE\n"    # data, not text
    )


def test_every_defined_function_is_offered_as_an_entry_point():
    """This test used to assert the opposite, and the thing it asserted was never measured.

    `analyze run --library` died on a C++ TU, and the diagnosis recorded here was that
    entry-point enumeration and IKOS's own preprocessing disagree about which WEAK
    definitions survive. It was reasoned, not observed — written on a machine with no
    clang, llvm-nm or ikos — and it is now measured and false. With IKOS 3.4 and clang-14,
    a TU with a `= default` ctor, an inline member and a template instantiation emits two
    `W` symbols; passing both to `ikos -e` exits 0 and finds real defects inside them. On
    the three analysable PX4 driver TUs there are no weak symbols at all, so the filter
    never fired on the corpus whose failure motivated it — it only cost the coverage of
    every inline and template function in a header.

    The real cause of that death was Clang emitting complete-object constructors as
    aliases; see tests/test_analysis_attribution.py.

    Parsed from real `llvm-nm` output text rather than by shelling out, because there is
    no clang, llvm-nm or ikos in this environment — a test that shelled out would silently
    skip and prove nothing.
    """
    from secagent.agents.analysis.ikos import defined_function_symbols

    entry_points, aliased = defined_function_symbols(_real_nm_output())
    assert entry_points == ["_ZN4Rtcm6decodeEPKhm", "_Z11rtcm_createv",
                            "_ZL15internal_helperv", "_ZN4RtcmC2Ev", "_ZN4RtcmD2Ev"]
    # C2/D2 are the base-object bodies. With no C1/D1 aliases present there is nothing to
    # resolve, and dropping them would be the coverage loss this change exists to end.
    assert aliased == []
    # Undefined and data symbols are not functions at all and were never entry points.
    assert "_ZSt4cerr" not in entry_points
    assert "_ZN4Rtcm5tableE" not in entry_points


def test_a_c_translation_unit_keeps_every_function():
    """The paired silence test: C has no implicit special members, so nothing is held
    back and the entry-point list is unchanged. A filter that dropped symbols from plain
    C would be a coverage regression wearing a bug fix's clothes."""
    from secagent.agents.analysis.ikos import defined_function_symbols

    strong, weak = defined_function_symbols(
        "---------------- T FM_AppMain\n"
        "---------------- T FM_AppInit\n"
        "---------------- t FM_LocalHelper\n"
    )
    assert strong == ["FM_AppMain", "FM_AppInit", "FM_LocalHelper"]
    assert weak == []


def test_a_refused_translation_unit_degrades_instead_of_raising(tmp_path, monkeypatch, caplog):
    """The headline: `analyze run --library` on `rtcm.cpp` raised straight out of
    `analyze_repo` as an unhandled traceback. `analyze scan` had always degraded per-TU;
    the single-target path was the one place that did not, so the same failure produced a
    clean "skipped, here is why" in one command and a stack trace in the other.

    A refused TU must produce a report that says so — never findings invented, never a
    traceback, and never an empty findings list that reads like a clean result.
    """
    from secagent.agents.analysis import agent as analysis_agent

    def boom(target, **kwargs):
        raise RuntimeError(
            "IKOS produced no report (exit 1): src/rtcm.cpp:14:3: error: "
            "entry point '_ZN4RtcmC2Ev' not found\nikos: error while running, abort.")

    monkeypatch.setattr(analysis_agent, "run_ikos", boom)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    tu = repo / "src" / "rtcm.cpp"
    tu.write_text("struct Rtcm { Rtcm() = default; };\n", encoding="utf-8")

    s = _settings(tmp_path)
    with caplog.at_level("WARNING"):
        result = analyze_repo(repo, s, target=str(tu), out_dir=tmp_path / "o", run=True)

    data = json.loads(Path(result["report_json"]).read_text())
    domain = data["coverage"]["translation_units"]
    assert domain["attempted"] == 1 and domain["failed"] == 1 and domain["succeeded"] == 0
    assert domain["complete"] is False
    reason = domain["why"]["src/rtcm.cpp"]["reason"]
    assert "entry point" in reason, f"the real diagnostic must survive, got {reason!r}"
    assert "abort" not in reason, "the useless trailer must not displace the diagnostic"
    assert data["summary"]["total"] == 0
    assert "NOT the same as finding none" in caplog.text


def test_an_analysed_translation_unit_reports_no_failure(tmp_path, monkeypatch, caplog):
    """Paired silence test: a TU IKOS accepts must report `succeeded`, an empty `why`,
    and no warning. A notice that always fires is one nobody reads."""
    from secagent.agents.analysis import agent as analysis_agent

    def ok(target, *, report_path, **kwargs):
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps({"runs": [{"results": []}]}),
                                     encoding="utf-8")
        return Path(report_path)

    monkeypatch.setattr(analysis_agent, "run_ikos", ok)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    tu = repo / "src" / "clean.c"
    tu.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    s = _settings(tmp_path)
    with caplog.at_level("WARNING"):
        result = analyze_repo(repo, s, target=str(tu), out_dir=tmp_path / "o", run=True)

    domain = result["coverage"]["translation_units"]
    assert domain["succeeded"] == 1 and domain["failed"] == 0
    assert domain["complete"] is True
    data = json.loads(Path(result["report_json"]).read_text())
    assert data["coverage"]["translation_units"]["why"] == {}
    assert "NOT the same as finding none" not in caplog.text


def test_ingest_still_has_no_translation_unit_domain(tmp_path):
    """Attack the mechanism: the single-target path can honestly say its population is
    one TU. `analyze ingest` cannot — the findings arrive from an external report and the
    TU population is invisible — so it must NOT gain a fabricated `translation_units`
    block as a side effect of this change."""
    report = tmp_path / "ikos.json"
    report.write_text(json.dumps([
        {"check": "boa", "result": "error", "filename": "src/a.c", "lineno": 3, "msg": "x"},
    ]), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    result = analyze_repo(repo, _settings(tmp_path), ikos_report=report,
                          out_dir=tmp_path / "o", run=False)
    assert set(result["coverage"]) == {"triage"}
