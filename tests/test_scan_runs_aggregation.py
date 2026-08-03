"""Scanning N times and aggregating, because a single run is a sample.

Measured: twelve runs over one file with one rule group produced a mean pairwise Jaccard
of 0.00 — no finding was ever reported twice, at either 0.7 or 1.0
(`quality/SCAN_REPRODUCIBILITY.md`). A user who reruns a scan gets a different answer, and
every precision figure taken from a single run is one draw from that process.

Aggregating over N runs fixes the user-facing half — the aggregate is stable even though
its inputs are not — and buys a confidence signal from data already paid for: a finding in
5 of 5 runs is almost certainly real, one in 1 of 5 almost certainly noise.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.scan.agent import _dedupe, scan_repo
from secagent.agents.scan.models import ScanFinding
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    s.scan.rule_granularity = "all"      # one call per file per run: easy to count
    return s


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    return repo


def _finding(line: int, rule: str = "BUF-001") -> str:
    return f'{{"rule":"{rule}","line":{line},"severity":"high","message":"m{line}"}}'


def _scripted(per_call: list[str]):
    """A model that returns a different finding set on each call, in order — the
    behaviour actually measured, not a stand-in that returns the same thing twice."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(per_call) - 1)
        calls["n"] += 1
        return httpx.Response(200, json=make_chat_response(content=per_call[i]))
    return mock_client(handler)


# --- the default must not move --------------------------------------------------

def test_a_single_run_is_still_the_default_and_looks_unchanged(tmp_path):
    """Silence test: `runs` defaults to 1, so an unconfigured scan makes exactly one call
    per file and every finding is trivially 1-of-1. Nobody's scan changes shape."""
    s = _settings(tmp_path)
    assert s.scan.runs == 1
    llm = _scripted([f"[{_finding(3)}]"])
    result = scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["runs"] == 1
    assert [f["runs_seen"] for f in data["findings"]] == [1]
    assert [f["run_fraction"] for f in data["findings"]] == [1.0]
    assert "filtered" not in data, "nothing may be filtered when no threshold is set"


# --- aggregation ----------------------------------------------------------------

def test_findings_carry_the_fraction_of_runs_that_saw_them(tmp_path):
    """The confidence signal. Three runs: one finding in all three, one in two, one in
    one — exactly the spread the reproducibility measurement found."""
    s = _settings(tmp_path)
    s.scan.runs = 3
    llm = _scripted([
        f"[{_finding(10)},{_finding(20)},{_finding(30)}]",
        f"[{_finding(10)},{_finding(20)}]",
        f"[{_finding(10)}]",
    ])
    result = scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["runs"] == 3
    by_line = {f["line"]: f for f in data["findings"]}
    assert set(by_line) == {10, 20, 30}, "the union of all runs is reported"
    assert (by_line[10]["runs_seen"], by_line[10]["run_fraction"]) == (3, 1.0)
    assert (by_line[20]["runs_seen"], by_line[20]["run_fraction"]) == (2, round(2 / 3, 4))
    assert (by_line[30]["runs_seen"], by_line[30]["run_fraction"]) == (1, round(1 / 3, 4))


def test_the_same_finding_in_every_run_is_reported_once(tmp_path):
    """Aggregation, not accumulation: N runs of an identical result must not multiply the
    finding count by N."""
    s = _settings(tmp_path)
    s.scan.runs = 4
    llm = _scripted([f"[{_finding(7)}]"])          # every call returns the same thing
    result = scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert len(data["findings"]) == 1
    assert data["findings"][0]["runs_seen"] == 4
    assert data["summary"]["total"] == 1


def test_the_aggregate_is_stable_even_though_the_runs_are_not(tmp_path):
    """The user-facing point. Two scans whose individual runs disagree completely — the
    measured behaviour — still produce the same aggregate, because the aggregate is the
    union over runs and the order runs complete in does not matter."""
    per_call = [f"[{_finding(10)}]", f"[{_finding(20)}]", f"[{_finding(30)}]"]
    seen = []
    for i in range(2):
        s = _settings(tmp_path / f"s{i}")
        s.scan.runs = 3
        result = scan_repo(_repo(tmp_path / f"r{i}"), s, out_dir=tmp_path / f"o{i}",
                           llm=_scripted(per_call))
        data = json.loads(Path(result["report_json"]).read_text())
        seen.append(sorted((f["line"], f["runs_seen"]) for f in data["findings"]))
    assert seen[0] == seen[1] == [(10, 1), (20, 1), (30, 1)]


# --- filtering must never be silent ---------------------------------------------

def test_a_threshold_removes_rare_findings_and_says_so(tmp_path, caplog):
    """A threshold that silently deletes findings is the failure the coverage contract
    exists to prevent. What is dropped is counted, broken down by rule, and warned about."""
    s = _settings(tmp_path)
    s.scan.runs = 3
    s.scan.min_run_fraction = 0.5
    llm = _scripted([
        f"[{_finding(10)},{_finding(30)}]",
        f"[{_finding(10)}]",
        f"[{_finding(10)}]",
    ])
    with caplog.at_level("WARNING"):
        result = scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert [f["line"] for f in data["findings"]] == [10], "the 1-of-3 finding is dropped"
    assert data["filtered"]["removed"] == 1
    assert data["filtered"]["min_run_fraction"] == 0.5
    assert data["filtered"]["by_rule"] == {"BUF-001": 1}
    assert "NOT listed" in caplog.text and "not the same as no finding" in caplog.text


def test_no_threshold_means_no_filtered_block_and_no_warning(tmp_path, caplog):
    """The paired silence test — a notice that always fires is one nobody reads."""
    s = _settings(tmp_path)
    s.scan.runs = 2
    llm = _scripted([f"[{_finding(10)},{_finding(30)}]", f"[{_finding(10)}]"])
    with caplog.at_level("WARNING"):
        result = scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert len(data["findings"]) == 2, "nothing is dropped by default"
    assert "filtered" not in data
    assert "NOT listed" not in caplog.text


# --- line drift ------------------------------------------------------------------

def _f(line: int, rule: str = "BUF-001") -> ScanFinding:
    return ScanFinding(rule_id=rule, category="buffer", severity="high",
                       file="a.c", line=line)


def test_exact_keying_keeps_drifted_lines_apart_by_default():
    """The deliberate choice. The model drifts line numbers between runs — a measured
    group reported 66/70/76/79/84 across three runs — so exact keying splits one
    wandering defect into several findings that each look rare, and `run_fraction` is a
    lower bound. The alternative, merging by proximity, would also fuse two genuinely
    distinct defects. Neither error is measured, so the default never merges."""
    assert [f.line for f in _dedupe([_f(66), _f(70), _f(76)], 0)] == [66, 70, 76]


def test_line_tolerance_merges_drift_when_asked_and_keeps_rules_apart():
    """Opting in merges same-file same-rule findings within the window, keeping the
    earliest line — but never merges across rules, which would hide a distinct defect."""
    assert [f.line for f in _dedupe([_f(66), _f(70), _f(84)], 5)] == [66, 84]
    merged = _dedupe([_f(66), _f(67, "PTR-001")], 5)
    assert sorted((f.rule_id, f.line) for f in merged) == [("BUF-001", 66), ("PTR-001", 67)]
