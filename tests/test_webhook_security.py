"""Tests for webhook auth hardening (CMMC-4)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from secagent.agents.review.webhook import create_app
from secagent.config import Settings

_IGNORED_MR = {
    "object_kind": "merge_request",
    "object_attributes": {"action": "close", "iid": 7},  # not reviewed -> no network
    "project": {"id": 42},
}


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_constant_time_token_accepts_valid():
    s = Settings()
    s.gitlab.webhook_secret = "s3cret"
    r = _client(s).post(
        "/webhook",
        headers={"X-Gitlab-Token": "s3cret", "X-Gitlab-Event": "Merge Request Hook"},
        json=_IGNORED_MR,
    )
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"


def test_token_rejected_when_wrong():
    s = Settings()
    s.gitlab.webhook_secret = "s3cret"
    r = _client(s).post("/webhook", headers={"X-Gitlab-Token": "nope"}, json=_IGNORED_MR)
    assert r.status_code == 401


def test_ip_allowlist_blocks_unknown_source():
    s = Settings()
    s.gitlab.webhook_secret = "s3cret"  # the receiver refuses to start without one
    s.gitlab.webhook_allowed_ips = ["10.0.0.1"]  # TestClient host is "testclient"
    # No token supplied: the IP check must reject first, so this is 403 and not 401.
    r = _client(s).post("/webhook", json=_IGNORED_MR)
    assert r.status_code == 403


def test_ip_allowlist_permits_listed_source():
    s = Settings()
    s.gitlab.webhook_secret = "s3cret"
    s.gitlab.webhook_allowed_ips = ["testclient"]  # the TestClient's reported host
    r = _client(s).post(
        "/webhook",
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s3cret"},
        json=_IGNORED_MR,
    )
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"
