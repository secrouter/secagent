"""Tests for `secagent review local`: reviewing a local git delta through the same
engine `review mr` uses, fed from a `gitscope.ChangeSet` instead of the GitLab API.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
from typer.testing import CliRunner

from secagent import gitscope
from secagent.agents.review.agent import review_local_changes, review_merge_request
from secagent.cli import app
from secagent.config import Settings
from secagent.llm.client import LLMClient, LLMResponse
from secagent.mcp.gitlab_harness import GitLabClient

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
ALIGN = REPO_ROOT / "config" / "alignment"
runner = CliRunner()


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


def _branch_repo(root: Path) -> Path:
    repo = _init_repo(root)
    _write(repo, "services/api/db.py", "def get_user(uid):\n    return {}\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature/cache")
    _write(repo, "services/api/db.py",
          "def get_user(uid):\n    return {}\n\n\ndef get_user_cached(uid):\n    return {}\n")
    _commit(repo, "add caching")
    return repo


def _settings() -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.persona.profile = str(ALIGN / "default.yaml")
    return s


# ---------------------------------------------------------------------------
# review_local_changes — direct
# ---------------------------------------------------------------------------


def test_review_local_reviews_the_delta_and_returns_no_posting_fields(tmp_path):
    repo = _branch_repo(tmp_path)
    s = _settings()
    s.affordances.store_dir = str(tmp_path / "store")
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: looks fine.\n- nit on db.py")))

    result = review_local_changes(s, repo=repo, llm=llm)
    assert result["changed_files"] == ["services/api/db.py"]
    assert "posted" not in result  # there is no MR, nothing was ever postable
    assert "note_id" not in result
    assert "secagent" in result["review"]
    assert result["scope"]["kind"] == "since_base"
    assert result["scope"]["base_ref"] == "main"
    assert result["persona"] == "default"


def test_review_local_defaults_to_since_base_when_no_scope_given(tmp_path):
    repo = _branch_repo(tmp_path)
    s = _settings()
    s.affordances.store_dir = str(tmp_path / "store")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="ok")))

    result = review_local_changes(s, repo=repo, llm=llm)  # no scope=
    assert result["scope"]["kind"] == "since_base"


def test_review_local_honors_an_explicit_scope(tmp_path):
    repo = _branch_repo(tmp_path)
    s = _settings()
    s.affordances.store_dir = str(tmp_path / "store")
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="ok")))

    cs = gitscope.staged(repo)  # nothing staged right now
    result = review_local_changes(s, repo=repo, scope=cs, llm=llm)
    assert result["changed_files"] == []
    assert result["scope"]["kind"] == "staged"


def test_review_local_diff_reaches_the_prompt(tmp_path):
    """The local diff must actually be in what the model sees — the whole point of
    feeding a `gitscope.ChangeSet` through `to_gitlab_style()`."""
    repo = _branch_repo(tmp_path)
    s = _settings()
    s.affordances.store_dir = str(tmp_path / "store")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler)
    review_local_changes(s, repo=repo, llm=llm)
    user_msg = seen["body"]["messages"][-1]["content"]
    assert "get_user_cached" in user_msg
    assert "services/api/db.py" in user_msg


# ---------------------------------------------------------------------------
# review mr and review local share one engine
# ---------------------------------------------------------------------------


def _gitlab_client(posted: list[dict]) -> GitLabClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/merge_requests/9"):
            return httpx.Response(200, json={
                "iid": 9, "title": "Add caching", "description": "",
                "source_branch": "feature/cache", "target_branch": "main",
            })
        if request.method == "GET" and path.endswith("/merge_requests/9/changes"):
            return httpx.Response(200, json={"changes": [
                {"new_path": "services/api/db.py", "old_path": "services/api/db.py",
                 "diff": "@@ -1,2 +1,5 @@\n def get_user(uid):\n     return {}\n"
                         "+\n+\n+def get_user_cached(uid):\n+    return {}\n"},
            ]})
        return httpx.Response(404, json={"error": path})

    cfg = Settings().gitlab
    cfg.url = "http://gl.example"
    cfg.token = "t"
    http = httpx.Client(
        base_url="http://gl.example/api/v4/", transport=httpx.MockTransport(handler))
    return GitLabClient(cfg, http=http)


def test_review_mr_and_review_local_produce_the_same_shape_of_review_text(tmp_path):
    """Not byte-identical (different diffs, different framing) — but both go through
    `_review_body`, so both come back with the marking/signature applied the same way
    and both are sensitive to the SAME diff content, proving neither forked the
    engine."""
    repo = _branch_repo(tmp_path)
    s = _settings()
    s.affordances.store_dir = str(tmp_path / "store")

    gl = _gitlab_client([])
    llm_mr = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: fine.")))
    mr_result = review_merge_request(
        s, project="42", mr_iid=9, post=False, gitlab=gl, llm=llm_mr)

    llm_local = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: fine.")))
    local_result = review_local_changes(s, repo=repo, llm=llm_local)

    for result in (mr_result, local_result):
        assert "Reviewed by secagent" in result["review"]
        assert result["persona"] == "default"
    assert mr_result["changed_files"] == local_result["changed_files"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _fake_review_chat(monkeypatch, content: str = "Summary: fine.\n- one nit") -> None:
    def fake_chat(self, messages, tools=None, temperature=None, max_tokens=None,
                 timeout=None) -> LLMResponse:
        return LLMResponse(content=content)

    monkeypatch.setattr(LLMClient, "chat", fake_chat)


def test_cli_review_local_prints_only_the_review_to_stdout(tmp_path, monkeypatch):
    repo = _branch_repo(tmp_path)
    _fake_review_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    monkeypatch.setenv("SECAGENT_PERSONA__PROFILE", str(ALIGN / "default.yaml"))

    result = runner.invoke(app, ["review", "local", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Summary: fine." in result.stdout
    assert "Reviewed by secagent" in result.stdout
    # No JSON, no scope metadata on stdout — that goes to stderr instead.
    assert not result.stdout.strip().startswith("{")
    assert "since_base" in result.stderr


def test_cli_review_local_honors_working_tree_flag(tmp_path, monkeypatch):
    repo = _branch_repo(tmp_path)
    _write(repo, "services/api/db.py", "def get_user(uid):\n    return {}\n# dirty\n")
    _fake_review_chat(monkeypatch)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    monkeypatch.setenv("SECAGENT_PERSONA__PROFILE", str(ALIGN / "default.yaml"))

    result = runner.invoke(app, ["review", "local", str(repo), "--working-tree"])
    assert result.exit_code == 0, result.output
    assert "working tree" in result.stderr


def test_cli_review_local_rejects_conflicting_scope_flags(tmp_path, monkeypatch):
    repo = _branch_repo(tmp_path)
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["review", "local", str(repo), "--staged", "--since", "main"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_cli_review_local_has_no_all_flag(tmp_path):
    result = runner.invoke(app, ["review", "local", "--help"])
    assert result.exit_code == 0
    assert "--all" not in result.stdout


def test_cli_review_local_reports_a_clear_error_for_a_non_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))

    result = runner.invoke(app, ["review", "local", str(plain)])
    assert result.exit_code == 1
    assert "not a git repository" in result.stdout
