"""Tests for UC4: LLM rule-based memory/stability scan."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.scan.agent import parse_findings, scan_repo
from secagent.agents.scan.report import render_markdown, summarize
from secagent.agents.scan.rules import load_rules, meets_threshold, rules_prompt
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "cpp_repo"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def test_rules_load_and_shape():
    rs = load_rules(RULES)
    assert rs.name == "embedded-cpp"
    ids = {r.id for r in rs.rules}
    assert {"MEM-001", "BUF-001", "ISR-001", "CTL-001"} <= ids
    assert rs.min_severity == "low"
    # rules_prompt lists rule ids for the model.
    prompt = rules_prompt(rs)
    assert "MEM-001" in prompt and "ISR-001" in prompt


def test_alternate_profile_is_scoped():
    rs = load_rules(REPO_ROOT / "config" / "rules" / "memory-critical.yaml")
    assert rs.min_severity == "high"
    assert all(r.severity in ("high", "critical") for r in rs.rules)


def test_profiles_declare_target_languages():
    # C profiles target C/C++; the Rust profile targets Rust.
    assert set(load_rules(RULES).languages) == {"C", "C++"}
    rust = load_rules(REPO_ROOT / "config" / "rules" / "rust-safety.yaml")
    assert rust.name == "rust-safety" and rust.languages == ["Rust"]
    assert {"PAN-001", "UNS-001", "INT-001"} <= {r.id for r in rust.rules}


def test_language_defaults_to_cpp_when_unspecified(tmp_path):
    # A profile with no `languages:` key stays C/C++ (back-compat).
    p = tmp_path / "p.yaml"
    p.write_text("name: t\nrules:\n  - id: X\n    severity: low\n")
    assert load_rules(p).languages == ["C", "C++"]


def test_select_files_filters_by_profile_language(tmp_path):
    from secagent.agents.scan.agent import _select_files

    (tmp_path / "a.c").write_text("int main(){}\n")
    (tmp_path / "b.rs").write_text("fn main() {}\n")
    s = _settings(tmp_path)
    # The Rust profile picks .rs and skips .c; a C profile does the reverse.
    # Returns (files, eligible_files) so a capped run can report what it never saw —
    # and which files, not merely how many.
    assert _select_files(tmp_path, s, None, {"Rust"}) == (["b.rs"], ["b.rs"])
    assert _select_files(tmp_path, s, None, {"C", "C++"}) == (["a.c"], ["a.c"])


def test_meets_threshold():
    assert meets_threshold("critical", "high")
    assert meets_threshold("high", "high")
    assert not meets_threshold("low", "high")


def test_parse_findings_filters_and_maps_category():
    rs = load_rules(RULES)
    rs.min_severity = "medium"
    text = """Here are the findings:
    ```json
    [
      {"rule": "BUF-001", "line": 8, "severity": "critical", "message": "strcpy overflow"},
      {"rule": "CTL-005", "line": 3, "severity": "low", "message": "add assert"}
    ]
    ```"""
    findings = parse_findings(text, "src/buf.c", rs)
    # CTL-005 (low) is filtered out by the medium threshold.
    assert len(findings) == 1
    assert findings[0].rule_id == "BUF-001"
    assert findings[0].category == "buffer"  # taken from the rule definition
    assert findings[0].line == 8


def test_parse_findings_tolerates_plain_array():
    rs = load_rules(RULES)
    findings = parse_findings('[{"rule":"PTR-001","line":5,"message":"null deref"}]',
                              "a.c", rs)
    assert len(findings) == 1
    assert findings[0].severity == "high"  # defaulted from the rule


def test_summarize_and_render():
    rs = load_rules(RULES)
    findings = parse_findings(
        '[{"rule":"MEM-003","line":8,"severity":"critical","message":"use after free"}]',
        "src/buf.c", rs)
    s = summarize(findings)
    assert s["total"] == 1 and s["critical"] == 1
    md = render_markdown(findings, project="x", ruleset_name=rs.name, summary=s,
                         banner="CUI")
    assert md.startswith("**CUI**")
    assert "MEM-003" in md and "src/buf.c:8" in md


def test_scan_repo_end_to_end_with_mock_llm(tmp_path):
    s = _settings(tmp_path)
    # The model returns one finding per file it is shown.
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='[{"rule":"BUF-001","line":8,"severity":"critical",'
                '"message":"strcpy into fixed buffer"}]')))
    out = tmp_path / "scan"
    result = scan_repo(FIXTURE, s, out_dir=out, llm=llm)
    assert result["files_scanned"] >= 1
    assert result["summary"]["total"] >= 1
    data = json.loads(Path(result["report_json"]).read_text())
    assert data["ruleset"] == "embedded-cpp"
    # Enrichment from the affordance store.
    assert any(f["component"] == "src" for f in data["findings"])
    assert any(f["rule_id"] == "BUF-001" for f in data["findings"])


def test_scan_repo_respects_max_files(tmp_path):
    """A positive cap bounds the run — the escape hatch for a quick pass."""
    s = _settings(tmp_path)
    s.scan.max_files = 1
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    result = scan_repo(FIXTURE, s, out_dir=tmp_path / "o", llm=llm)
    assert result["files_scanned"] == 1


def test_scan_defaults_to_the_whole_project(tmp_path):
    """0 means every file, not none.

    A memory-safety scanner that quietly stops at file 50 reports an all-clear it did not
    earn for everything after it — the same false-clean failure as an unreported analysis
    error, just arriving by a different route. Scanning everything is the default; the cap
    is opt-in for when you want speed over completeness.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    s = _settings(tmp_path)
    s.scan.max_files = 0
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    assert scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)["files_scanned"] == 5

    capped = _settings(tmp_path)
    capped.scan.max_files = 2
    llm2 = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    assert scan_repo(repo, capped, out_dir=tmp_path / "o2",
                     llm=llm2)["files_scanned"] == 2
