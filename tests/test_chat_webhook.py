"""Tests for the Mattermost chat webhook receiver (UC101, CMMC-4 auth hardening).

Mirrors test_webhook_security.py's shape for the GitLab receiver.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from secagent.chat.mattermost import MattermostClient
from secagent.chat.router import MattermostAuthError
from secagent.chat.webhook import create_app
from secagent.config import Settings


def _settings(tmp_path, **overrides) -> Settings:
    s = Settings()
    s.mattermost.webhook_secret = "s3cret"
    s.mattermost.bot_username = "secagent"
    for k, v in overrides.items():
        setattr(s.mattermost, k, v)
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "audit.jsonl")
    return s


def _mock_mattermost(handler):
    return MattermostClient(
        Settings().mattermost,
        http=httpx.Client(base_url="http://mock/api/v4/", transport=httpx.MockTransport(handler)),
    )


def _client(settings: Settings, mattermost: MattermostClient | None = None) -> TestClient:
    return TestClient(create_app(settings, mattermost=mattermost))


# -- startup: fail-closed webhook auth (mirrors GitLab's WebhookAuthError) ----------


def test_missing_secret_refuses_to_start():
    s = Settings()  # mattermost.webhook_secret defaults to ""
    with pytest.raises(MattermostAuthError, match="webhook_secret"):
        create_app(s)


def test_unauthenticated_opt_in_allows_start():
    s = Settings()
    s.mattermost.webhook_allow_unauthenticated = True
    create_app(s)  # should not raise


def test_healthz_reports_bot_username(tmp_path):
    s = _settings(tmp_path)
    r = _client(s).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "bot": "secagent"}


# -- inbound auth: constant-time token check ----------------------------------------


def test_correct_token_slash_command_form_routes_and_replies(tmp_path):
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "post123"})

    s = _settings(tmp_path)
    mm = _mock_mattermost(handler)
    r = _client(s, mattermost=mm).post(
        "/webhook",
        data={
            "token": "s3cret",
            "team_id": "t1",
            "team_domain": "devteam",
            "channel_id": "chan1",
            "user_id": "u1",
            "user_name": "alice",
            "command": "/secagent",
            "text": "help",
            "trigger_id": "trig1",
        },
    )
    assert r.status_code == 200
    assert r.json()["action"] == "help"
    assert r.json()["posted"] is True
    # No `text` in the synchronous response — the reply was already posted via REST,
    # echoing it here too would double-post it in Mattermost's UI.
    assert "text" not in r.json()
    assert "secagent commands" in posted["body"]["message"]
    assert posted["body"]["channel_id"] == "chan1"


def test_correct_token_outgoing_webhook_json_body_routes_and_replies(tmp_path):
    """Outgoing webhooks can be configured to POST JSON instead of form data, and
    their text includes the mention (unlike a slash command's)."""
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "post456"})

    s = _settings(tmp_path)
    mm = _mock_mattermost(handler)
    r = _client(s, mattermost=mm).post(
        "/webhook",
        headers={"content-type": "application/json"},
        json={
            "token": "s3cret",
            "team_id": "t1",
            "channel_id": "chan2",
            "user_id": "u9",
            "user_name": "bob",
            "post_id": "post-root-1",
            "text": "@secagent help",
            "trigger_word": "@secagent",
        },
    )
    assert r.status_code == 200
    assert r.json()["action"] == "help"
    assert posted["body"]["root_id"] == "post-root-1"  # threaded on the triggering post


def test_wrong_token_rejected():
    s = Settings()
    s.mattermost.webhook_secret = "s3cret"
    r = _client(s).post("/webhook", data={"token": "nope", "text": "help"})
    assert r.status_code == 401


def test_missing_token_rejected_when_secret_configured():
    s = Settings()
    s.mattermost.webhook_secret = "s3cret"
    r = _client(s).post("/webhook", data={"text": "help"})
    assert r.status_code == 401


def test_unauthenticated_opt_in_accepts_any_token(tmp_path):
    s = _settings(tmp_path, webhook_secret="", webhook_allow_unauthenticated=True)
    r = _client(s).post("/webhook", data={"text": "help", "user_name": "alice"})
    assert r.status_code == 200
    assert r.json()["action"] == "help"


# -- source-IP allow-list (defense-in-depth, mirrors GitLab's) ----------------------


def test_ip_allowlist_blocks_unknown_source(tmp_path):
    s = _settings(tmp_path, webhook_allowed_ips=["10.0.0.1"])  # TestClient host is "testclient"
    r = _client(s).post("/webhook", data={"token": "s3cret", "text": "help"})
    assert r.status_code == 403


def test_ip_allowlist_permits_listed_source(tmp_path):
    s = _settings(tmp_path, webhook_allowed_ips=["testclient"])  # TestClient's reported host
    r = _client(s).post("/webhook", data={"token": "s3cret", "text": "help"})
    assert r.status_code == 200


# -- ignoring the bot's own posts ---------------------------------------------------


def test_own_message_ignored_no_post(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not post when ignoring the bot's own message")

    s = _settings(tmp_path)
    mm = _mock_mattermost(handler)
    r = _client(s, mattermost=mm).post(
        "/webhook", data={"token": "s3cret", "user_name": "secagent", "text": "hi"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"


# -- a dispatch-level failure must not 500 the whole endpoint -----------------------


def test_dispatch_failure_reports_error_without_500(tmp_path, monkeypatch):
    import secagent.chat.webhook as webhook_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(webhook_module, "dispatch_chat_event", boom)
    s = _settings(tmp_path)
    r = _client(s).post("/webhook", data={"token": "s3cret", "text": "help"})
    assert r.status_code == 200
    assert r.json()["action"] == "error"
    assert "simulated failure" in r.json()["error"]
