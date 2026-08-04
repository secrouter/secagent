"""Tests for UC1 docs' git-delta scoping: `build_docs(scope=...)` and the CLI's
`docs build` `--base`/`--since`/`--staged`/`--working-tree`/`--path`/`--all` flags.

Docs has no per-file model call to skip outright the way `scan` skips a whole file:
the affordance store is always fully (re)indexed and the rendered site always has a
page for every file. What scoping narrows is which files' PURPOSE SUMMARY / function
descriptions are worth a fresh model call this run — so these tests are built around
proving two things together: the model is only ever asked about delta files, AND the
site/manifest still account for every file in the repository. `--all` (scope=None)
must reproduce today's whole-repo behavior byte-for-byte, so several tests assert the
ABSENCE of anything scope-related, not just the presence of the new behavior when
scoped — the same discipline `test_scan_gitscope.py` uses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from secagent import gitscope
from secagent.affordances.store import AffordanceStore
from secagent.agents.docs.agent import build_docs
from secagent.cli import app
from secagent.config import Settings
from secagent.llm.client import LLMClient, LLMResponse

runner = CliRunner()


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = True
    s.affordances.store_dir = str(tmp_path / "store")
    # Isolate file-purpose scoping (this page's subject) from the separate
    # per-function description pass, which is scoped identically but exercised by
    # its own existing tests, not duplicated here.
    s.affordances.max_function_docs = 0
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


def _branch_repo_with_one_new_file(root: Path) -> Path:
    """main has one old.py; feature adds a new one."""
    repo = _init_repo(root)
    _write(repo, "src/old.py", "def old_fn():\n    return 1\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/new.py", "def new_fn():\n    return 2\n")
    _commit(repo, "add new.py")
    return repo


def _purpose_marker(path: str) -> str:
    """The one substring that identifies a call as `_llm_purpose`'s FILE-PURPOSE
    prompt for ``path`` specifically (see `affordances/file_summary.py`) — as
    opposed to `outline.py`'s component/overview/architecture PROSE prompts, which
    legitimately mention every file's path too (as grounding context; docs never
    scopes that). Matching this instead of a bare path is what keeps these tests
    from false-positiving on that unrelated, always-unscoped call.
    """
    return f"File: {path} ("


def _fake_chat_by_marker(monkeypatch, calls: list[str], purposes: dict[str, str]) -> None:
    """Patch `LLMClient.chat`: return ``purposes[marker]`` for whichever marker
    string appears in the last (user) message, and record every user message seen
    in ``calls`` — so a test can assert a path's file-purpose prompt was, or was
    NEVER, sent to the model (see `_purpose_marker`).
    """

    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        user = messages[-1]["content"]
        calls.append(user)
        for marker, text in purposes.items():
            if marker in user:
                return LLMResponse(content=text)
        return LLMResponse(content="")

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


# ---------------------------------------------------------------------------
# build_docs(scope=...) — direct
# ---------------------------------------------------------------------------


def test_scoped_build_calls_the_model_only_for_delta_files(tmp_path, monkeypatch):
    """A fresh (never-before-indexed) repo: old.py is outside the delta and has no
    prior cache to reuse, so it must be indexed HEURISTICALLY — the model must never
    be asked about it — while new.py, inside the delta, gets a real model call."""
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    calls: list[str] = []
    _fake_chat_by_marker(monkeypatch, calls, {
        _purpose_marker("src/old.py"): "OLD PURPOSE TEXT.",
        _purpose_marker("src/new.py"): "NEW PURPOSE TEXT.",
    })

    out = tmp_path / "docs"
    report = build_docs(repo, out, s, run_sphinx=False, scope=cs)

    assert not any(_purpose_marker("src/old.py") in c for c in calls), (
        "src/old.py is outside the delta and must never get a file-purpose call")
    assert any(_purpose_marker("src/new.py") in c for c in calls)

    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        summaries = store.all_summaries()
        assert summaries["src/new.py"].source == "llm"
        assert summaries["src/new.py"].purpose == "NEW PURPOSE TEXT."
        # Still indexed (the site stays complete) but never handed to the model.
        assert summaries["src/old.py"].source == "heuristic"
        assert len(store.file_records()) == 2   # the WHOLE repo, not just the delta
    finally:
        store.close()

    scope = report["scope"]
    assert scope["kind"] == "since_base"
    assert scope["base_ref"] == "main"
    assert scope["partial"] is True
    assert scope["total_analyzable"] == 2
    assert scope["refreshed"] == 1   # only new.py
    assert scope["reused"] == 1      # old.py


def test_scoped_build_describes_functions_only_for_delta_files(tmp_path, monkeypatch):
    """The per-function LLM description pass (`_describe_functions`) is scoped the
    same way the file-purpose pass is — exercised here with its own distinct prompt
    shape ("One-sentence description:") so it isn't just incidentally covered by the
    file-purpose assertions above.
    """
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    s.affordances.max_function_docs = 10   # re-enable; disabled by default in _settings
    cs = gitscope.since_base(repo)

    calls: list[str] = []

    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        user = messages[-1]["content"]
        calls.append(user)
        if "One-sentence purpose:" in user:
            return LLMResponse(content="A purpose.")
        return LLMResponse(content="A function description.")

    monkeypatch.setattr(LLMClient, "chat", fake_chat)

    build_docs(repo, tmp_path / "docs", s, run_sphinx=False, scope=cs)

    fn_calls = [c for c in calls if "One-sentence description:" in c]
    assert fn_calls, "expected at least one function-description call"
    assert all("new_fn" in c for c in fn_calls), (
        "only new.py's function may be described this run")
    assert not any("old_fn" in c for c in fn_calls), (
        "old.py is outside the delta and must not get a function description")

    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        new_syms = store.symbols_for_file("src/new.py")
        old_syms = store.symbols_for_file("src/old.py")
    finally:
        store.close()
    assert any(sym.doc for sym in new_syms), "new.py's function should have a description"
    assert not any(sym.doc for sym in old_syms), (
        "old.py's function must have no description — it was never asked about")


def test_scoped_build_reuses_a_previously_cached_llm_summary(tmp_path, monkeypatch):
    """Unlike the fresh-repo case above, old.py already has a REAL cached LLM
    summary from a previous (unscoped) build — the scoped run must leave it exactly
    as it was, neither regenerating it nor downgrading it to heuristic.
    """
    repo = _init_repo(tmp_path)
    _write(repo, "src/old.py", "def old_fn():\n    return 1\n")
    _commit(repo, "initial")

    s = _settings(tmp_path)
    seed_calls: list[str] = []
    _fake_chat_by_marker(monkeypatch, seed_calls,
                         {_purpose_marker("src/old.py"): "ORIGINAL PURPOSE TEXT."})
    build_docs(repo, tmp_path / "seed", s, run_sphinx=False)   # unscoped: seeds the cache

    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/new.py", "def new_fn():\n    return 2\n")
    _commit(repo, "add new.py")

    calls: list[str] = []
    _fake_chat_by_marker(monkeypatch, calls, {
        _purpose_marker("src/old.py"): (
            "THIS MUST NEVER BE SEEN — old.py is out of scope this run."),
        _purpose_marker("src/new.py"): "REFRESHED PURPOSE TEXT.",
    })
    cs = gitscope.since_base(repo)
    report = build_docs(repo, tmp_path / "docs2", s, run_sphinx=False, scope=cs)

    assert not any(_purpose_marker("src/old.py") in c for c in calls)

    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        summaries = store.all_summaries()
        assert summaries["src/old.py"].source == "llm"
        assert summaries["src/old.py"].purpose == "ORIGINAL PURPOSE TEXT."  # untouched
        assert summaries["src/new.py"].purpose == "REFRESHED PURPOSE TEXT."
    finally:
        store.close()

    assert report["scope"]["refreshed"] == 1
    assert report["scope"]["reused"] == 1
    assert report["scope"]["total_analyzable"] == 2


def test_scoped_build_site_and_manifest_stay_complete(tmp_path, monkeypatch):
    """The rendered site (component pages) and summaries.json must list EVERY file —
    including the reused one — even though only the delta was refreshed."""
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    _fake_chat_by_marker(monkeypatch, [], {
        _purpose_marker("src/old.py"): "OLD PURPOSE TEXT.",
        _purpose_marker("src/new.py"): "NEW PURPOSE TEXT.",
    })

    out = tmp_path / "docs"
    report = build_docs(repo, out, s, run_sphinx=False, scope=cs)

    components_rst = Path(report["write"]["source_dir"], "components.rst").read_text()
    assert "old.py" in components_rst
    assert "new.py" in components_rst

    manifest = json.loads(Path(report["summaries_manifest"]["json"]).read_text())
    assert "src/old.py" in manifest["files"]
    assert "src/new.py" in manifest["files"]
    assert manifest["scope"]["partial"] is True
    assert manifest["scope"]["refreshed"] == 1
    assert manifest["scope"]["total_analyzable"] == 2

    summaries_md = Path(out, "summaries.md").read_text()
    assert "SCOPED RUN" in summaries_md
    assert "PARTIAL" in summaries_md


def test_scoped_build_emits_the_coverage_banner(tmp_path, monkeypatch, caplog):
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    cs = gitscope.since_base(repo)
    _fake_chat_by_marker(monkeypatch, [], {
        _purpose_marker("src/old.py"): "OLD.", _purpose_marker("src/new.py"): "NEW.",
    })

    with caplog.at_level("WARNING"):
        build_docs(repo, tmp_path / "docs", s, run_sphinx=False, scope=cs)

    assert "SCOPED run" in caplog.text
    assert "PARTIAL" in caplog.text
    assert "since_base" in caplog.text


def test_scope_none_reproduces_legacy_output_with_no_scope_key(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    calls: list[str] = []
    _fake_chat_by_marker(monkeypatch, calls, {
        _purpose_marker("src/old.py"): "OLD PURPOSE TEXT.",
        _purpose_marker("src/new.py"): "NEW PURPOSE TEXT.",
    })

    out = tmp_path / "docs"
    report = build_docs(repo, out, s, run_sphinx=False)   # no scope= at all

    assert "scope" not in report
    # Whole-repo: BOTH files reach the model when unscoped.
    assert any(_purpose_marker("src/old.py") in c for c in calls)
    assert any(_purpose_marker("src/new.py") in c for c in calls)

    manifest = json.loads(Path(report["summaries_manifest"]["json"]).read_text())
    assert "scope" not in manifest

    summaries_md = Path(out, "summaries.md").read_text()
    assert "SCOPED RUN" not in summaries_md


def test_all_scope_explicitly_none_matches_omitting_the_argument(tmp_path, monkeypatch):
    """`--all` resolves to `scope=None` via `gitscope.resolve_scope`; confirm passing
    that sentinel explicitly behaves identically to not passing `scope` at all."""
    repo = _branch_repo_with_one_new_file(tmp_path)
    s = _settings(tmp_path)
    _fake_chat_by_marker(monkeypatch, [], {
        "src/old.py": "OLD.", "src/new.py": "NEW.",
    })

    report = build_docs(repo, tmp_path / "docs", s, run_sphinx=False, scope=None)
    assert "scope" not in report


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _fake_docs_chat(monkeypatch, content: str = "A purpose.") -> None:
    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        return LLMResponse(content=content)

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


def test_cli_default_docs_build_is_scoped(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_file(tmp_path)
    _fake_docs_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__MAX_FUNCTION_DOCS", "0")

    result = runner.invoke(
        app, ["docs", "build", str(repo), "-o", str(tmp_path / "out"), "--no-build"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["scope"]["kind"] == "since_base"
    assert data["scope"]["partial"] is True
    assert data["scope"]["total_analyzable"] == 2


def test_cli_docs_build_all_reproduces_the_original_whole_repo_behavior(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_file(tmp_path)
    _fake_docs_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__MAX_FUNCTION_DOCS", "0")

    result = runner.invoke(
        app, ["docs", "build", str(repo), "--all", "-o", str(tmp_path / "out"), "--no-build"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "scope" not in data


def test_cli_docs_build_path_flows_through_a_changeset(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_file(tmp_path)
    _fake_docs_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__MAX_FUNCTION_DOCS", "0")

    result = runner.invoke(
        app, ["docs", "build", str(repo), "--path", "src/old.py",
              "-o", str(tmp_path / "out"), "--no-build"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["scope"]["kind"] == "explicit"


def test_cli_docs_build_rejects_conflicting_scope_flags(tmp_path, monkeypatch):
    repo = _branch_repo_with_one_new_file(tmp_path)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(
        app, ["docs", "build", str(repo), "--all", "--staged", "--no-build"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_cli_docs_build_reports_a_clear_error_for_a_non_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["docs", "build", str(plain), "--no-build"])
    assert result.exit_code == 1
    assert "not a git repository" in result.stdout
