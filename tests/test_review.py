"""Tests for UC100: GitLab harness, persona, review agent, triggers, webhook."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from secagent.agents.review.agent import respond_to_mention, review_merge_request
from secagent.agents.review.persona import load_persona
from secagent.agents.review.triggers import dispatch_event, is_mention
from secagent.config import Settings
from secagent.mcp.gitlab_harness import GitLabClient

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
ALIGN = REPO_ROOT / "config" / "alignment"


def gitlab_client(posted: list[dict]) -> GitLabClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path.endswith("/merge_requests"):
            return httpx.Response(200, json=[{"iid": 7, "title": "Add caching layer"}])
        if method == "GET" and path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={
                "iid": 7, "title": "Add caching layer",
                "description": "Introduce a cache.", "source_branch": "feat/cache",
                "target_branch": "main",
            })
        if method == "GET" and path.endswith("/merge_requests/7/changes"):
            return httpx.Response(200, json={"changes": [
                {"new_path": "services/api/db.py", "old_path": "services/api/db.py",
                 "diff": "@@ -1,2 +1,3 @@\n-x\n+y\n+z\n"},
            ]})
        if method == "POST" and path.endswith("/merge_requests/7/notes"):
            posted.append({"kind": "note", "body": _form_body(request)})
            return httpx.Response(201, json={"id": 555})
        if method == "POST" and "/discussions/" in path:
            posted.append({"kind": "reply", "body": _form_body(request)})
            return httpx.Response(201, json={"id": 556})
        return httpx.Response(404, json={"error": path})

    cfg = Settings().gitlab
    cfg.url = "http://gl.example"
    cfg.token = "t"
    http = httpx.Client(
        base_url="http://gl.example/api/v4/", transport=httpx.MockTransport(handler)
    )
    return GitLabClient(cfg, http=http)


def _form_body(request: httpx.Request) -> str:
    from urllib.parse import parse_qs

    return parse_qs(request.content.decode())["body"][0]


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.persona.profile = str(ALIGN / "default.yaml")
    s.gitlab.bot_username = "secagent-bot"
    return s


def test_persona_loads_and_shapes_prompt():
    p = load_persona(ALIGN / "default.yaml")
    assert p.name == "default"
    assert "correctness" in p.system_prompt().lower()

    strict = load_persona(ALIGN / "security-strict.yaml")
    assert strict.verbosity == "detailed"
    assert strict.max_inline_comments == 15
    # Verbosity changes the directive text -> different prompts.
    assert p.system_prompt() != strict.system_prompt()


def test_is_mention():
    assert is_mention("hey @secagent-bot please look", "secagent-bot")
    assert not is_mention("no mention here", "secagent-bot")


def test_review_merge_request_posts_note(settings):
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: looks good.\n- Check error handling.")))
    result = review_merge_request(
        settings, project="42", mr_iid=7, post=True, gitlab=gl, llm=llm)
    assert result["posted"] is True
    assert result["note_id"] == 555
    assert "services/api/db.py" in result["changed_files"]
    assert len(posted) == 1
    assert "secagent" in posted[0]["body"]


def test_review_dry_run_does_not_post(settings):
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="review")))
    result = review_merge_request(
        settings, project="42", mr_iid=7, post=False, gitlab=gl, llm=llm)
    assert result["posted"] is False
    assert posted == []


def test_verbosity_changes_review_prompt(settings, captured_requests):
    factory, store = captured_requests
    # Run with terse vs detailed personas; assert the system prompt differs.
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = factory(make_chat_response(content="ok"))
    settings.persona.profile = str(ALIGN / "default.yaml")
    review_merge_request(settings, project="42", mr_iid=7, post=False, gitlab=gl, llm=llm)
    default_sys = store[-1]["messages"][0]["content"]

    posted2: list[dict] = []
    gl2 = gitlab_client(posted2)
    llm2 = factory(make_chat_response(content="ok"))
    settings.persona.profile = str(ALIGN / "security-strict.yaml")
    review_merge_request(settings, project="42", mr_iid=7, post=False, gitlab=gl2, llm=llm2)
    strict_sys = store[-1]["messages"][0]["content"]

    assert default_sys != strict_sys
    assert "security" in strict_sys.lower()


def test_respond_to_mention_posts_reply(settings):
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Good question — the cache TTL is 60s.")))
    result = respond_to_mention(
        settings, project="42", mr_iid=7, discussion_id="abc123",
        thread="@secagent-bot what's the TTL?", post=True, gitlab=gl, llm=llm)
    assert result["posted"] is True
    assert result["note_id"] == 556
    assert posted[0]["kind"] == "reply"


def test_dispatch_mr_event_reviews(settings):
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="review body")))
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 7},
        "project": {"id": 42},
    }
    report = dispatch_event(settings, "Merge Request Hook", payload, gitlab=gl, llm=llm)
    assert report["action"] == "reviewed"
    assert report["posted"] is True


def test_dispatch_note_without_mention_ignored(settings):
    payload = {
        "object_kind": "note",
        "object_attributes": {"noteable_type": "MergeRequest", "note": "nice work",
                              "discussion_id": "d1"},
        "merge_request": {"iid": 7},
        "project": {"id": 42},
        "user": {"username": "alice"},
    }
    report = dispatch_event(settings, "Note Hook", payload)
    assert report["action"] == "ignored"


def test_dispatch_note_with_mention_replies(settings):
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="reply")))
    payload = {
        "object_kind": "note",
        "object_attributes": {"noteable_type": "MergeRequest",
                              "note": "@secagent-bot thoughts?", "discussion_id": "d1"},
        "merge_request": {"iid": 7},
        "project": {"id": 42},
        "user": {"username": "alice"},
    }
    report = dispatch_event(settings, "Note Hook", payload, gitlab=gl, llm=llm)
    assert report["action"] == "replied"
    assert posted[0]["kind"] == "reply"


def test_poll_reviews_new_open_mrs_once(settings):
    from secagent.agents.review.webhook import poll_open_merge_requests

    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Summary: looks good.")))
    seen = poll_open_merge_requests(
        settings, "42", seen=set(), once=True, gitlab=gl, llm=llm)
    # The one open MR (iid 7) was reviewed and recorded as seen.
    assert seen == {7}
    assert len(posted) == 1 and posted[0]["kind"] == "note"

    # A second pass with the MR already seen posts nothing more.
    seen = poll_open_merge_requests(
        settings, "42", seen=seen, once=True, gitlab=gl, llm=llm)
    assert seen == {7}
    assert len(posted) == 1


def test_webhook_token_and_routing(settings):
    from fastapi.testclient import TestClient

    from secagent.agents.review.webhook import create_app

    settings.gitlab.webhook_secret = "s3cret"
    app = create_app(settings)
    client = TestClient(app)

    assert client.get("/healthz").json()["status"] == "ok"

    # Bad token -> 401.
    bad = client.post("/webhook", headers={"X-Gitlab-Token": "nope"}, json={})
    assert bad.status_code == 401

    # Good token, non-review action -> ignored, no network.
    payload = {"object_kind": "merge_request",
               "object_attributes": {"action": "close", "iid": 7},
               "project": {"id": 42}}
    ok = client.post("/webhook", headers={"X-Gitlab-Token": "s3cret",
                     "X-Gitlab-Event": "Merge Request Hook"}, json=payload)
    assert ok.status_code == 200
    assert ok.json()["action"] == "ignored"


def test_gitlab_harness_url_building():
    posted: list[dict] = []
    gl = gitlab_client(posted)
    # group/name path projects must be URL-encoded in the request path.
    mr = gl.get_merge_request("42", 7)
    assert mr["iid"] == 7
