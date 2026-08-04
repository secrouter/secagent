"""Tests for UC5 testgen's git-delta scoping: `generate_tests(scope=...)` and the
CLI's `testgen` `--base`/`--since`/`--staged`/`--working-tree`/`--path`/`--all` flags.

WHERE tests are written never changes with scoping (always the same side tree); what
scoping narrows is WHICH targets get a fresh generated test — the unit pass to files
in the delta, the functional pass to components that own at least one delta file. As
in `test_scan_gitscope.py`, `--all` (scope=None) must reproduce today's whole-repo
behavior byte-for-byte, so several tests assert the ABSENCE of anything
scope-related, not just the presence of the new behavior when scoped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
from typer.testing import CliRunner

from secagent import gitscope
from secagent.agents.testgen.agent import generate_tests
from secagent.cli import app
from secagent.config import Settings
from secagent.llm.client import LLMClient, LLMResponse

from .conftest import make_chat_response, mock_client

runner = CliRunner()


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _llm(content: str = "```python\ndef test_generated():\n    assert True\n```"):
    return mock_client(lambda r: httpx.Response(200, json=make_chat_response(content=content)))


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


def _branch_repo_with_two_components(root: Path) -> Path:
    """main has one file each in two components (compA, compB); feature adds a
    second file to compA only — so compA "touches the delta" and compB does not.
    """
    repo = _init_repo(root)
    _write(repo, "src/compA/a1.py", "def a1_fn(x):\n    return x + 1\n")
    _write(repo, "src/compB/b1.py", "def b1_fn(x):\n    return x + 2\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/compA/a2.py", "def a2_fn(x):\n    return x + 3\n")
    _commit(repo, "add a2.py")
    return repo


# ---------------------------------------------------------------------------
# generate_tests(scope=...) — direct
# ---------------------------------------------------------------------------


def test_scoped_unit_pass_targets_only_delta_files(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)

    result = generate_tests(repo, s, out_dir=tmp_path / "out", functional=False,
                            llm=_llm(), scope=cs)

    targets = {g["target"] for g in result["generated"]}
    assert targets == {"src/compA/a2.py"}, (
        "only the delta's own file may be a unit target, even though a1.py sits in "
        "the same (in-scope) component")


def test_scoped_functional_pass_targets_only_components_touching_the_delta(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)

    result = generate_tests(repo, s, out_dir=tmp_path / "out", unit=False,
                            llm=_llm(), scope=cs)

    targets = {g["target"] for g in result["generated"]}
    assert targets == {"compA"}
    assert "compB" not in targets, "compB owns no delta file and must not be a target"


def test_scope_block_reports_in_scope_vs_total_for_both_domains(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)

    result = generate_tests(repo, s, out_dir=tmp_path / "out", llm=_llm(), scope=cs)

    scope = result["scope"]
    assert scope["kind"] == "since_base"
    assert scope["base_ref"] == "main"
    assert scope["partial"] is True
    assert scope["unit_files_in_scope"] == 1                    # a2.py only
    assert scope["unit_files_total_analyzable"] == 3             # a1, a2, b1
    assert scope["functional_components_in_scope"] == 1          # compA only
    assert scope["functional_components_total_analyzable"] == 2  # compA, compB


def test_manifest_and_readme_record_the_scope_as_partial(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    out = tmp_path / "out"

    generate_tests(repo, s, out_dir=out, llm=_llm(), scope=cs)

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["scope"]["partial"] is True
    assert manifest["scope"]["unit_files_in_scope"] == 1
    assert manifest["scope"]["functional_components_in_scope"] == 1

    readme = (out / "README.md").read_text()
    assert "Scoped run" in readme
    assert "--all" in readme


def test_scoped_run_emits_the_coverage_banner(tmp_path, caplog):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)

    with caplog.at_level("WARNING"):
        generate_tests(repo, s, out_dir=tmp_path / "out", llm=_llm(), scope=cs)

    assert "SCOPED run" in caplog.text
    assert "PARTIAL" in caplog.text
    assert "since_base" in caplog.text


def test_scope_none_reproduces_legacy_output_with_no_scope_key(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)
    out = tmp_path / "out"

    result = generate_tests(repo, s, out_dir=out, llm=_llm())   # no scope= at all

    assert "scope" not in result
    targets = {g["target"] for g in result["generated"]}
    assert targets == {
        "src/compA/a1.py", "src/compA/a2.py", "src/compB/b1.py", "compA", "compB",
    }

    manifest = json.loads((out / "manifest.json").read_text())
    assert "scope" not in manifest
    readme = (out / "README.md").read_text()
    assert "Scoped run" not in readme


def test_all_scope_explicitly_none_matches_omitting_the_argument(tmp_path):
    repo = _branch_repo_with_two_components(tmp_path)
    s = _settings(tmp_path)

    result = generate_tests(repo, s, out_dir=tmp_path / "out", llm=_llm(), scope=None)
    assert "scope" not in result


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _fake_testgen_chat(monkeypatch,
                       content: str = "```python\ndef test_x():\n    assert True\n```") -> None:
    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        return LLMResponse(content=content)

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


def test_cli_default_testgen_is_scoped(tmp_path, monkeypatch):
    repo = _branch_repo_with_two_components(tmp_path)
    _fake_testgen_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")

    result = runner.invoke(app, ["testgen", str(repo), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["scope"]["kind"] == "since_base"
    assert data["scope"]["partial"] is True


def test_cli_testgen_all_reproduces_the_original_whole_repo_behavior(tmp_path, monkeypatch):
    repo = _branch_repo_with_two_components(tmp_path)
    _fake_testgen_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")

    result = runner.invoke(app, ["testgen", str(repo), "--all", "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "scope" not in data


def test_cli_testgen_path_flows_through_a_changeset(tmp_path, monkeypatch):
    repo = _branch_repo_with_two_components(tmp_path)
    _fake_testgen_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")

    result = runner.invoke(
        app, ["testgen", str(repo), "--path", "src/compA/a1.py", "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["scope"]["kind"] == "explicit"


def test_cli_testgen_rejects_conflicting_scope_flags(tmp_path, monkeypatch):
    repo = _branch_repo_with_two_components(tmp_path)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["testgen", str(repo), "--all", "--staged"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_cli_testgen_reports_a_clear_error_for_a_non_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["testgen", str(plain)])
    assert result.exit_code == 1
    assert "not a git repository" in result.stdout
