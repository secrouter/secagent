"""UC100 runs unattended, with a token that can post publicly. These are the properties
that have to hold when nobody is watching.

Every test here corresponds to something an evaluation agent verified live against the
real code: an unauthenticated webhook, triplicate review comments from the cron pattern
our own docs recommend, one transient GitLab 500 taking down a whole poll pass, and the
internal model endpoint's host:port appearing in a public merge-request comment.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from secagent.agents.review.agent import review_merge_request
from secagent.agents.review.webhook import (
    WebhookAuthError,
    create_app,
    poll_open_merge_requests,
)
from secagent.config import Settings
from secagent.mcp.gitlab_harness import GitLabClient

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
ALIGN = REPO_ROOT / "config" / "alignment"

_IGNORED_MR = {
    "object_kind": "merge_request",
    "object_attributes": {"action": "close", "iid": 7},  # not reviewed -> no network
    "project": {"id": 42},
}


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.persona.profile = str(ALIGN / "default.yaml")
    s.gitlab.bot_username = "secagent-bot"
    return s


# --------------------------------------------------------------------------------------
# 1. The webhook must not serve without authentication.
# --------------------------------------------------------------------------------------


def test_webhook_refuses_to_serve_without_a_secret():
    """`webhook_secret` defaulted to "" and the auth check was skipped entirely when it
    was unset, so an operator who missed one env var deployed an open POST endpoint that
    would review-and-post to any project the GitLab token could reach. A default that
    silently disables authentication is the same class of defect as a silent all-clear."""
    s = Settings()
    assert s.gitlab.webhook_secret == "", "the empty default is the whole point"

    with pytest.raises(WebhookAuthError, match="webhook_secret"):
        create_app(s)


def test_the_unauthenticated_escape_hatch_is_explicit_and_works():
    """Paired silence. Refusing to start is only correct if there is a way to say "yes, I
    really do mean an open endpoint" — otherwise someone deletes the check. The opt-out
    must be a deliberate, named setting, never a consequence of leaving a field blank."""
    s = Settings()
    s.gitlab.webhook_allow_unauthenticated = True

    app = create_app(s)  # must not raise
    r = TestClient(app).post(
        "/webhook",
        headers={"X-Gitlab-Event": "Merge Request Hook"},  # no token at all
        json=_IGNORED_MR,
    )
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"


def test_a_configured_secret_is_still_enforced():
    """The fix must not become "we check at startup, so we can stop checking requests"."""
    s = Settings()
    s.gitlab.webhook_secret = "s3cret"
    client = TestClient(create_app(s))

    assert client.post("/webhook", headers={"X-Gitlab-Token": "nope"},
                       json=_IGNORED_MR).status_code == 401
    assert client.post("/webhook", json=_IGNORED_MR).status_code == 401


# --------------------------------------------------------------------------------------
# 2. `review poll --once` is the cron pattern our own docs recommend.
# --------------------------------------------------------------------------------------


def _gitlab(posted: list[dict], *, mrs=(7,), fail_note_for: set[int] = frozenset()):
    """A mock GitLab serving `mrs` as open, optionally 500ing note creation for some."""

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"iid": i, "title": f"MR {i}"} for i in mrs])
        for iid in mrs:
            if method == "GET" and path.endswith(f"/merge_requests/{iid}"):
                return httpx.Response(200, json={
                    "iid": iid, "title": f"MR {iid}", "description": "d",
                    "source_branch": "f", "target_branch": "main"})
            if method == "GET" and path.endswith(f"/merge_requests/{iid}/changes"):
                return httpx.Response(200, json={"changes": [
                    {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n"}]})
            if method == "POST" and path.endswith(f"/merge_requests/{iid}/notes"):
                if iid in fail_note_for:
                    return httpx.Response(500, text="GitLab is having a moment")
                posted.append({"iid": iid})
                return httpx.Response(201, json={"id": 500 + iid})
        return httpx.Response(404, json={"error": path})

    cfg = Settings().gitlab
    cfg.url, cfg.token = "http://gl.example", "t"
    return GitLabClient(cfg, http=httpx.Client(
        base_url="http://gl.example/api/v4/", transport=httpx.MockTransport(handler)))


def _reviewing_llm():
    return mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: looks good.")))


def test_cron_ticks_do_not_repost_the_same_review(settings, tmp_path):
    """Three simulated cron ticks against one open MR produced three duplicate public
    review comments on someone's merge request. `seen` lived only in memory and the CLI
    never passed or kept it, so every `--once` invocation started blank and every open MR
    looked new. This is steady-state behaviour of the documented pattern, not a restart
    edge case."""
    posted: list[dict] = []
    state = tmp_path / "seen.json"

    for _ in range(3):  # three cron ticks: no shared memory between them, by construction
        poll_open_merge_requests(
            settings, "42", once=True, state_path=state,
            gitlab=_gitlab(posted), llm=_reviewing_llm())

    assert [p["iid"] for p in posted] == [7], "one MR reviewed once, not three times"
    assert state.exists(), "the seen-set has to outlive the process to survive cron"


def test_the_persisted_seen_set_is_per_project(settings, tmp_path):
    """Attack the mechanism: a single flat set would let project A's iid 7 suppress the
    review of project B's iid 7, which is a silent no-review — the failure mode this
    project exists to prevent, arriving through the fix for a different one."""
    posted: list[dict] = []
    state = tmp_path / "seen.json"

    poll_open_merge_requests(settings, "project-a", once=True, state_path=state,
                             gitlab=_gitlab(posted), llm=_reviewing_llm())
    poll_open_merge_requests(settings, "project-b", once=True, state_path=state,
                             gitlab=_gitlab(posted), llm=_reviewing_llm())

    assert len(posted) == 2, "the same iid in a different project is a different MR"
    assert set(json.loads(state.read_text())) == {"project-a", "project-b"}


def test_an_explicit_seen_argument_takes_precedence_over_the_file(settings, tmp_path):
    """Paired silence: a caller that manages `seen` itself must get exactly the set it
    asked for. If the file quietly won, an in-memory caller would inherit another run's
    history — and if the file were quietly written, the long-running loop would start
    competing with cron for the same state."""
    posted: list[dict] = []
    state = tmp_path / "seen.json"
    state.write_text(json.dumps({"42": [7]}))  # the file says MR 7 is already done

    seen = poll_open_merge_requests(
        settings, "42", seen=set(), once=True, state_path=state,
        gitlab=_gitlab(posted), llm=_reviewing_llm())

    assert seen == {7} and len(posted) == 1, "the caller's empty set must win, not the file"
    assert json.loads(state.read_text()) == {"42": [7]}, "the file must be left alone"


# --------------------------------------------------------------------------------------
# 3. One flaky MR must not take down the whole pass.
# --------------------------------------------------------------------------------------


def test_one_failing_mr_does_not_abort_the_others(settings, tmp_path, caplog):
    """A transient GitLab 500 on one MR propagated out of the poll loop uncaught, killing
    the pass for every other watched MR — and under the long-running mode, the process.
    The webhook route already wraps the identical `dispatch_event` call; the poll loop
    did not."""
    posted: list[dict] = []
    state = tmp_path / "seen.json"

    with caplog.at_level("ERROR"):
        poll_open_merge_requests(
            settings, "42", once=True, state_path=state,
            gitlab=_gitlab(posted, mrs=(7, 8), fail_note_for={7}),
            llm=_reviewing_llm())

    assert [p["iid"] for p in posted] == [8], "MR 8 must still be reviewed"
    assert "7" in caplog.text, "the failure must be reported, not swallowed"
    # The failed MR is NOT recorded as seen, so the next tick retries it. Recording it
    # would turn a transient GitLab hiccup into a permanently unreviewed merge request.
    assert json.loads(state.read_text())["42"] == [8]


def test_a_retried_failure_is_reviewed_not_skipped_forever(settings, tmp_path):
    """The other half of the above: once GitLab recovers, the MR that failed gets its
    review. A fix that isolates the error by marking the MR done would be worse than the
    crash it replaced."""
    posted: list[dict] = []
    state = tmp_path / "seen.json"

    poll_open_merge_requests(settings, "42", once=True, state_path=state,
                             gitlab=_gitlab(posted, mrs=(7,), fail_note_for={7}),
                             llm=_reviewing_llm())
    assert posted == []

    poll_open_merge_requests(settings, "42", once=True, state_path=state,
                             gitlab=_gitlab(posted, mrs=(7,)), llm=_reviewing_llm())
    assert [p["iid"] for p in posted] == [7]


# --------------------------------------------------------------------------------------
# 4. LLM failure text is posted publicly.
# --------------------------------------------------------------------------------------


def test_llm_failure_does_not_leak_the_endpoint_into_a_public_comment(settings):
    """`httpx.HTTPStatusError.__str__` embeds the full request URL, and that string was
    returned verbatim as the review body and posted to the merge request. A 4xx from the
    model server therefore published internal network topology — host and port of the
    model endpoint — under the bot identity on someone's MR."""
    settings.llm.base_url = "http://internal-llm-host.corp:9009/v1"
    settings.llm.max_retries = 1
    posted: list[dict] = []

    def gl_handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"iid": 7, "title": "t", "description": "d"})
        if method == "GET" and path.endswith("/merge_requests/7/changes"):
            return httpx.Response(200, json={"changes": [
                {"new_path": "a.py", "old_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n"}]})
        if method == "POST" and path.endswith("/notes"):
            from urllib.parse import parse_qs
            posted.append({"body": parse_qs(request.content.decode())["body"][0]})
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"error": path})

    cfg = settings.gitlab
    cfg.url, cfg.token = "http://gl.example", "t"
    gl = GitLabClient(cfg, http=httpx.Client(
        base_url="http://gl.example/api/v4/", transport=httpx.MockTransport(gl_handler)))
    llm = mock_client(lambda r: httpx.Response(400, json={"error": "bad request"}))

    review_merge_request(settings, project="42", mr_iid=7, post=True, gitlab=gl, llm=llm)

    body = posted[0]["body"]
    assert "internal-llm-host.corp" not in body
    assert "9009" not in body
    assert "chat/completions" not in body
    # Still discloses. Suppressing the message must not become suppressing the failure —
    # a comment that silently omits the failure would read as a clean review.
    assert "could not generate a review" in body


# --------------------------------------------------------------------------------------
# 5. The reviewer posts prose publicly and never grounded it.
# --------------------------------------------------------------------------------------


def _repo_with_index(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real_module.py").write_text("def real_function():\n    return 1\n")
    return repo


def _review_body(settings, repo: Path, review_text: str) -> str:
    posted: list[dict] = []

    def gl_handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"iid": 7, "title": "t", "description": "d"})
        if method == "GET" and path.endswith("/merge_requests/7/changes"):
            return httpx.Response(200, json={"changes": [
                {"new_path": "real_module.py", "old_path": "real_module.py",
                 "diff": "@@ -1 +1 @@\n-x\n+y\n"}]})
        if method == "POST" and path.endswith("/notes"):
            from urllib.parse import parse_qs
            posted.append({"body": parse_qs(request.content.decode())["body"][0]})
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"error": path})

    cfg = settings.gitlab
    cfg.url, cfg.token = "http://gl.example", "t"
    gl = GitLabClient(cfg, http=httpx.Client(
        base_url="http://gl.example/api/v4/", transport=httpx.MockTransport(gl_handler)))
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content=review_text)))

    review_merge_request(settings, project="42", mr_iid=7, repo=repo, post=True,
                         gitlab=gl, llm=llm)
    return posted[0]["body"]


def test_a_review_citing_a_file_that_does_not_exist_is_flagged(settings, tmp_path):
    """Triage, which nobody reads publicly, grounds its claims. The reviewer, which posts
    prose on someone else's merge request, did not — zero references to `grounding` in the
    whole review agent. That is backwards on risk: a wrong comment on someone's code costs
    another person's time and credibility."""
    body = _review_body(
        settings, _repo_with_index(tmp_path),
        "Summary: the change is fine.\n\n- The helper in `imaginary_module.py` duplicates "
        "this logic and should be reused instead.")

    assert "UNVERIFIED" in body
    assert "imaginary_module.py" in body


def test_a_review_citing_only_real_things_is_not_annotated(settings, tmp_path):
    """Paired silence. A checker that fires on correct output is a checker people learn to
    scroll past — which is how it stops being read at all."""
    body = _review_body(
        settings, _repo_with_index(tmp_path),
        "Summary: the change is fine.\n\n- `real_module.py` already covers this.")

    assert "UNVERIFIED" not in body
    assert "real_module.py" in body


def test_grounding_never_removes_or_rewrites_the_review(settings, tmp_path):
    """Attack the mechanism: the check must annotate, not edit. A grounding pass that
    silently dropped an unverifiable finding would convert a checkable wrong claim into an
    invisible missing one."""
    review = ("Summary: two problems.\n\n- `imaginary_module.py` is wrong.\n"
              "- `real_module.py` is also wrong.")
    body = _review_body(settings, _repo_with_index(tmp_path), review)

    for line in review.splitlines():
        assert line in body, "every line of the model's review must survive verbatim"
    assert "UNVERIFIED" in body, "and the reader must be told which reference did not resolve"
