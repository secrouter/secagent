"""Tests for UC4 scan's git-delta scoping: `scan_repo(scope=...)` and the CLI's
`--base`/`--since`/`--staged`/`--working-tree`/`--path`/`--all` flags.

`--all` must reproduce today's whole-repo behavior byte-for-byte, so several tests
here assert the ABSENCE of anything scope-related, not just the presence of the new
behavior when scoped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
from typer.testing import CliRunner

from secagent import gitscope
from secagent.agents.scan.agent import scan_repo
from secagent.cli import app
from secagent.config import Settings
from secagent.llm.client import LLMClient, LLMResponse

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"
runner = CliRunner()


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _branch_repo_with_one_new_c_file(root: Path) -> Path:
    """main has one old .c file; feature adds a new one with an obvious bug."""
    repo = _init_repo(root)
    _write(repo, "src/old.c", "int add(int a, int b) { return a + b; }\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/new.c",
          '#include <string.h>\nvoid f(char *d, const char *s) { strcpy(d, s); }\n')
    _commit(repo, "add new.c")
    return repo


# ---------------------------------------------------------------------------
# scan_repo(scope=...) — direct
# ---------------------------------------------------------------------------


def test_scoped_run_scans_only_the_delta_and_reports_the_scope_block(tmp_path):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='[{"rule":"BUF-001","line":2,"severity":"high","message":"strcpy"}]')))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)

    assert result["files_scanned"] == 1  # only src/new.c, not src/old.c
    scope = result["scope"]
    assert scope["kind"] == "since_base"
    assert scope["base_ref"] == "main"
    assert scope["in_scope_files"] == 1
    assert scope["total_analyzable"] == 2  # both old.c and new.c exist in the repo
    assert scope["partial"] is True
    assert result["analysis_complete"] is False  # a scoped run is never "complete"

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["scope"] == scope
    assert "src/new.c" in {f["file"] for f in data["findings"]}
    assert "src/old.c" not in {f["file"] for f in data["findings"]}


def test_scoped_run_emits_the_coverage_banner(tmp_path, caplog):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    with caplog.at_level("WARNING"):
        scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)

    assert "SCOPED run" in caplog.text
    assert "PARTIAL" in caplog.text
    assert "since_base" in caplog.text


def test_scoped_run_markdown_carries_the_scoped_admonition(tmp_path):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)
    md = Path(result["report_md"]).read_text()
    assert "SCOPED RUN" in md
    assert "PARTIAL" in md


def test_scope_that_touches_no_ruleset_language_file_selects_nothing(tmp_path):
    """A delta that only changed a non-C/C++ file must select NOTHING — not silently
    fall back to a full-repo walk. `_select_files` treats an empty explicit path list
    the same as "no list given"; scan_repo must not let that leak through here."""
    repo = _init_repo(tmp_path)
    _write(repo, "src/old.c", "int f(void) { return 0; }\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "README.md", "# docs only change\n")
    _commit(repo, "docs only")

    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)
    assert result["files_scanned"] == 0
    assert result["scope"]["in_scope_files"] == 0
    assert result["scope"]["dropped_non_analyzable"] == 1  # README.md, not a ruleset language
    assert result["scope"]["total_analyzable"] == 1  # src/old.c exists in the repo


def test_max_files_cap_applies_within_the_scoped_delta(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "README.md", "placeholder\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    for i in range(4):
        _write(repo, f"src/m{i}.c", f"int f{i}(void) {{ return {i}; }}\n")
    _commit(repo, "add four files")

    s = _settings(tmp_path)
    s.scan.max_files = 2
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)
    assert result["files_scanned"] == 2
    assert result["files_skipped_by_cap"] == 2
    assert result["scope"]["in_scope_files"] == 4  # the cap is a SEPARATE dimension


def test_dropped_non_analyzable_counts_both_generic_and_language_drops(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/old.c", "int f(void) { return 0; }\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/new.c", "int g(void) { return 1; }\n")
    _write(repo, "README.md", "docs\n")  # wrong language
    _write(repo, "build/generated.c", "// build artifact, ignored by default globs\n")
    _commit(repo, "mixed")

    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", scope=cs, llm=llm)
    assert result["scope"]["in_scope_files"] == 1  # only src/new.c
    assert result["scope"]["dropped_non_analyzable"] == 2  # README.md + build/generated.c


# ---------------------------------------------------------------------------
# scope=None — legacy / --all must be untouched, byte-for-byte
# ---------------------------------------------------------------------------


def test_scope_none_reproduces_legacy_output_with_no_scope_key(tmp_path):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    s = _settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", llm=llm)  # no scope=, no paths=
    assert "scope" not in result
    assert result["files_scanned"] == 2  # BOTH old.c and new.c: the whole repo
    assert result["analysis_complete"] is True

    data = json.loads(Path(result["report_json"]).read_text())
    assert "scope" not in data


def test_explicit_paths_kwarg_without_scope_is_also_untouched(tmp_path):
    """The pre-existing `paths=` escape hatch for a direct Python caller must keep
    behaving exactly as before scoping existed — no scope block, no forced
    `analysis_complete=False`."""
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    s = _settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))

    result = scan_repo(repo, s, out_dir=tmp_path / "out", paths=["src/old.c"], llm=llm)
    assert "scope" not in result
    assert result["files_scanned"] == 1
    assert result["analysis_complete"] is True


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _fake_scan_chat(monkeypatch, content: str = "[]") -> None:
    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        return LLMResponse(content=content)

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


def test_cli_default_scan_is_scoped(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    _fake_scan_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    monkeypatch.setenv("SECAGENT_SCAN__RULES_PROFILE", str(RULES))

    result = runner.invoke(
        app, ["scan", str(repo), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["files_scanned"] == 1
    assert data["scope"]["kind"] == "since_base"
    assert data["analysis_complete"] is False


def test_cli_all_reproduces_the_original_whole_repo_behavior(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    _fake_scan_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    monkeypatch.setenv("SECAGENT_SCAN__RULES_PROFILE", str(RULES))

    result = runner.invoke(
        app, ["scan", str(repo), "--all", "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "scope" not in data
    assert data["files_scanned"] == 2
    assert data["analysis_complete"] is True


def test_cli_path_flows_through_a_changeset(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    _fake_scan_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    monkeypatch.setenv("SECAGENT_SCAN__RULES_PROFILE", str(RULES))

    result = runner.invoke(
        app, ["scan", str(repo), "--path", "src/old.c", "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["scope"]["kind"] == "explicit"
    assert data["files_scanned"] == 1


def test_cli_rejects_conflicting_scope_flags(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_c_file(tmp_path)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["scan", str(repo), "--all", "--staged"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_cli_reports_a_clear_error_for_a_non_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.c").write_text("int main(void) { return 0; }\n")
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["scan", str(plain)])
    assert result.exit_code == 1
    assert "not a git repository" in result.stdout
