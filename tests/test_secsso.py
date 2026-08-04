"""Tests for the SecSSO client_credentials token helper (secagent token / secsso.py)."""

from __future__ import annotations

import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from secagent.config import SecSSOConfig
from secagent.secsso import SecSSOError, fetch_token, get_token


def _config(tmp_path, **overrides) -> SecSSOConfig:
    defaults = {
        "token_url": "https://secsso.example.com/realms/secrouter/protocol/openid-connect/token",
        "client_id": "secagent",
        "username": "svc-secagent",
        "client_secret_env": "SECAGENT_TEST_CLIENT_SECRET",
        "scope": "openid secrouter",
        "token_cache_path": str(tmp_path / "secsso-token.json"),
        "expiry_buffer_s": 60.0,
    }
    defaults.update(overrides)
    return SecSSOConfig(**defaults)


def _token_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_response(access_token: str = "tok-abc123", expires_in: int = 300):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": access_token, "expires_in": expires_in})
    return handler


# -- fetch_token: the raw client_credentials grant ---------------------------------


def test_fetch_token_sends_correct_grant_and_returns_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": "tok-xyz", "expires_in": 300})

    config = _config(tmp_path)
    result = fetch_token(config, http=_token_client(handler))

    assert result.access_token == "tok-xyz"
    assert captured["url"] == config.token_url
    assert captured["body"]["grant_type"] == ["client_credentials"]
    assert captured["body"]["client_id"] == ["secagent"]
    assert captured["body"]["client_secret"] == ["s3cret"]
    assert captured["body"]["scope"] == ["openid secrouter"]
    # username is informational only — never sent as a grant parameter (RFC 6749
    # client_credentials carries no resource-owner identity).
    assert "username" not in captured["body"]


def test_fetch_token_missing_token_url_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    config = _config(tmp_path, token_url="")
    with pytest.raises(SecSSOError, match="token_url"):
        fetch_token(config)


def test_fetch_token_missing_client_secret_env_config_raises(tmp_path):
    config = _config(tmp_path, client_secret_env="")
    with pytest.raises(SecSSOError, match="client_secret_env"):
        fetch_token(config)


def test_fetch_token_unset_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SECAGENT_TEST_CLIENT_SECRET", raising=False)
    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="SECAGENT_TEST_CLIENT_SECRET"):
        fetch_token(config)


def test_fetch_token_http_error_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "wrong")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="401"):
        fetch_token(config, http=_token_client(handler))


def test_fetch_token_no_access_token_in_response_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})  # malformed: no access_token

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="access_token"):
        fetch_token(config, http=_token_client(handler))


# -- get_token: the file cache in front of fetch_token ------------------------------


def test_get_token_cache_miss_fetches_and_writes_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 300})

    config = _config(tmp_path)
    token = get_token(config, http=_token_client(handler), now=1000.0)

    assert token == "fresh-token"
    assert calls["n"] == 1
    cache_path = tmp_path / "secsso-token.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["access_token"] == "fresh-token"
    assert data["expires_at"] == pytest.approx(1000.0 + 300)


def test_get_token_cache_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    config = _config(tmp_path)
    get_token(config, http=_token_client(_ok_response()), now=1000.0)
    mode = stat.S_IMODE((tmp_path / "secsso-token.json").stat().st_mode)
    assert mode == 0o600


def test_get_token_cache_hit_makes_no_network_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    cache_path = tmp_path / "secsso-token.json"
    cache_path.write_text(json.dumps({"access_token": "cached-token", "expires_at": 10_000.0}))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called on a cache hit")

    config = _config(tmp_path)
    # now=1000, expires_at=10000, buffer=60 -> 9000s of headroom, well cached.
    token = get_token(config, http=_token_client(handler), now=1000.0)
    assert token == "cached-token"


def test_get_token_refreshes_when_within_expiry_buffer(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    cache_path = tmp_path / "secsso-token.json"
    # now=1000, expires_at=1030 -> only 30s left, buffer is 60s -> must refresh.
    cache_path.write_text(json.dumps({"access_token": "stale-token", "expires_at": 1030.0}))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "refreshed-token", "expires_in": 300})

    config = _config(tmp_path)
    token = get_token(config, http=_token_client(handler), now=1000.0)

    assert token == "refreshed-token"
    assert calls["n"] == 1
    assert json.loads(cache_path.read_text())["access_token"] == "refreshed-token"


def test_get_token_refresh_failure_raises_and_leaves_stale_cache_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    cache_path = tmp_path / "secsso-token.json"
    cache_path.write_text(json.dumps({"access_token": "stale-token", "expires_at": 1030.0}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    config = _config(tmp_path)
    with pytest.raises(SecSSOError):
        get_token(config, http=_token_client(handler), now=1000.0)
    # A failed refresh must not corrupt/clear the existing cache file.
    assert json.loads(cache_path.read_text())["access_token"] == "stale-token"


def test_get_token_ignores_corrupt_cache_and_refetches(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_CLIENT_SECRET", "s3cret")
    cache_path = tmp_path / "secsso-token.json"
    cache_path.write_text("{ not valid json")

    config = _config(tmp_path)
    token = get_token(config, http=_token_client(_ok_response("recovered")), now=1000.0)
    assert token == "recovered"
