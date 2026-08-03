"""The scanner must not claim a capability it was measured not to have.

The `embedded-cpp` profile declares `languages: [C, C++]`. Measured, it finds real
defects in C and none in C++: 0 true positives in 32 findings on the PX4 GNSS parsers,
and one real defect reported INVERTED — it recommended adding a NULL check after `new`,
which is dead code, at the site where that idiom is itself the bug
(`quality/PRECISION_CPP.md`). On flight software, acting on that makes the code worse.

C++ is warned about rather than removed. Removing it from the profile would make
`_select_files` match nothing, and the scan would print "No findings against the
configured rules" over a codebase it never read — a silent false all-clear, which is the
single most dangerous output this tool can produce and the thing it exists to prevent.
A wrong answer with its reliability attached beats a clean-looking answer to a question
nobody asked.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.scan.agent import scan_repo
from secagent.agents.scan.report import render_markdown
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    s.scan.rule_granularity = "all"
    return s


def _llm():
    return mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="[]")))


def test_the_profile_still_accepts_cpp(tmp_path):
    """The capability claim stays, deliberately. If this ever becomes C-only, a C++ scan
    selects zero files and reports a clean result for code it never opened."""
    from secagent.agents.scan.rules import load_rules

    assert "C++" in load_rules(RULES).languages


def test_a_cpp_scan_says_the_results_are_not_reliable(tmp_path, caplog):
    """The disclosure, in both places a reader looks: the log and `scan.md`. The report
    matters more — stderr scrolls away, the artifact is what gets read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.cpp").write_text("int main() { return 0; }\n")

    with caplog.at_level("WARNING"):
        result = scan_repo(repo, _settings(tmp_path), out_dir=tmp_path / "o", llm=_llm())

    assert "C++" in caplog.text and "24%" in caplog.text
    assert "1 run in 3" in caplog.text, \
        "recall is the part precision does not fix, and must be stated"

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_cpp"] == 1
    md = Path(result["report_md"]).read_text()
    assert "C++ IS NOT RECOMMENDED" in md
    assert "1 run in 3" in md


def test_a_c_only_scan_says_nothing_about_cpp(tmp_path, caplog):
    """The paired silence test. A notice that fires on every scan is one nobody reads,
    and C is the language the tool was measured to be useful for."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    with caplog.at_level("WARNING"):
        result = scan_repo(repo, _settings(tmp_path), out_dir=tmp_path / "o", llm=_llm())

    assert "C++" not in caplog.text
    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_cpp"] == 0
    assert "NOT RECOMMENDED" not in Path(result["report_md"]).read_text()


def test_the_banner_is_driven_by_the_summary():
    """Same contract as every other banner in this report: the renderer reads a count
    off the summary rather than re-deriving it, so one place decides."""
    md = render_markdown([], project="p", ruleset_name="r", summary={"files_cpp": 3})
    assert "C++ IS NOT RECOMMENDED — 3 C++ file(s)" in md
    assert "NOT RECOMMENDED" not in render_markdown(
        [], project="p", ruleset_name="r", summary={"files_cpp": 0})
