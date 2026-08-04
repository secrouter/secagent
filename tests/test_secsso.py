"""Tests for secagent's two SecSSO token helpers (secsso.py):

- service identity (``secagent token`` / ``client_credentials``): ``fetch_token`` /
  ``get_token``.
- per-user identity (``secagent login`` / ``logout`` / ``token --user`` / OIDC device
  authorization, RFC 8628): ``login`` / ``get_user_token`` / ``logout`` /
  ``peek_user_token`` / ``refresh_user_token`` / ``request_device_code``.
"""

from __future__ import annotations

import base64
import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from secagent.config import SecSSOConfig
from secagent.secsso import (
    DeviceAuthorization,
    SecSSOError,
    fetch_token,
    get_token,
    get_user_token,
    login,
    logout,
    peek_user_token,
    refresh_user_token,
    request_device_code,
)


def _config(tmp_path, **overrides) -> SecSSOConfig:
    defaults = {
        "token_url": "https://secsso.example.com/realms/secrouter/protocol/openid-connect/token",
        "client_id": "secagent",
        "username": "svc-secagent",
        "client_secret_env": "SECAGENT_TEST_CLIENT_SECRET",
        "scope": "openid secrouter",
        "token_cache_path": str(tmp_path / "secsso-token.json"),
        "expiry_buffer_s": 60.0,
        "device_authorization_url": "https://secsso.example.com/application/o/device/",
        "device_client_id": "secagent-pi",
        "device_scope": "openid profile email secrouter",
        "user_token_cache_path": str(tmp_path / "user-token.json"),
    }
    defaults.update(overrides)
    return SecSSOConfig(**defaults)


def _fake_id_token(**claims) -> str:
    """A structurally-valid (unsigned, UNVERIFIED-on-purpose) JWT for exercising
    `_id_token_claims`'s display-only decode -- not a real/trusted token."""
    def b64(obj) -> str:
        raw = json.dumps(obj).encode() if not isinstance(obj, (bytes, str)) else (
            obj.encode() if isinstance(obj, str) else obj)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64(claims)
    return f"{header}.{payload}.sig"


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


# ════════════════════════════════════════════════════════════════════════════════════
# Per-user identity: OIDC device authorization (RFC 8628) — secagent login/logout,
# `secagent token --user`.
# ════════════════════════════════════════════════════════════════════════════════════


# -- request_device_code -------------------------------------------------------------


def test_request_device_code_sends_client_id_and_scope(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={
            "device_code": "devcode-1", "user_code": "ABCD-1234",
            "verification_uri": "https://secsso.example.com/if/device/",
            "verification_uri_complete": "https://secsso.example.com/if/device/?code=ABCD-1234",
            "expires_in": 600, "interval": 5,
        })

    config = _config(tmp_path)
    device = request_device_code(config, http=_token_client(handler))

    assert captured["url"] == config.device_authorization_url
    assert captured["body"]["client_id"] == ["secagent-pi"]
    assert captured["body"]["scope"] == ["openid profile email secrouter"]
    # No secret of any kind is ever sent — this is a PUBLIC client (RFC 8628 SS3.1).
    assert "client_secret" not in captured["body"]
    assert device == DeviceAuthorization(
        device_code="devcode-1", user_code="ABCD-1234",
        verification_uri="https://secsso.example.com/if/device/",
        verification_uri_complete="https://secsso.example.com/if/device/?code=ABCD-1234",
        expires_in=600.0, interval=5.0,
    )


def test_request_device_code_missing_url_raises(tmp_path):
    config = _config(tmp_path, device_authorization_url="")
    with pytest.raises(SecSSOError, match="device_authorization_url"):
        request_device_code(config)


def test_request_device_code_http_error_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="400"):
        request_device_code(config, http=_token_client(handler))


def test_request_device_code_missing_fields_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"device_code": "x"})  # no user_code/verification_uri

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="missing"):
        request_device_code(config, http=_token_client(handler))


# -- login: the full device-code dance ------------------------------------------------


def _device_response(**overrides) -> dict:
    body = {
        "device_code": "devcode-1", "user_code": "ABCD-1234",
        "verification_uri": "https://secsso.example.com/if/device/",
        "verification_uri_complete": "https://secsso.example.com/if/device/?code=ABCD-1234",
        "expires_in": 600, "interval": 1,
    }
    body.update(overrides)
    return body


def test_login_pending_then_success_writes_cache_and_prompts_once(tmp_path):
    calls = {"device": 0, "token": 0}
    id_token = _fake_id_token(sub="user-42", email="dev@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            calls["device"] += 1
            return httpx.Response(200, json=_device_response())
        calls["token"] += 1
        if calls["token"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json={
            "access_token": "user-access-tok", "refresh_token": "user-refresh-tok",
            "expires_in": 3600, "id_token": id_token,
        })

    prompts = []
    config = _config(tmp_path)
    result = login(
        config, http=_token_client(handler), now=1000.0, sleep=lambda s: None,
        on_prompt=prompts.append,
    )

    assert result.access_token == "user-access-tok"
    assert result.refresh_token == "user-refresh-tok"
    assert result.expires_at == pytest.approx(1000.0 + 3600)
    assert result.sub == "user-42"
    assert result.email == "dev@example.com"
    assert calls["device"] == 1        # device code requested exactly once
    assert calls["token"] == 3         # 2 pending + 1 success
    assert len(prompts) == 1
    assert prompts[0].user_code == "ABCD-1234"

    cache_path = tmp_path / "user-token.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["access_token"] == "user-access-tok"
    assert data["refresh_token"] == "user-refresh-tok"
    assert data["sub"] == "user-42"
    assert data["email"] == "dev@example.com"


def test_login_cache_file_is_owner_only(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    config = _config(tmp_path)
    login(config, http=_token_client(handler), now=1000.0, sleep=lambda s: None)
    mode = stat.S_IMODE((tmp_path / "user-token.json").stat().st_mode)
    assert mode == 0o600


def test_login_slow_down_keeps_polling_until_success(tmp_path):
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        calls["token"] += 1
        if calls["token"] == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    config = _config(tmp_path)
    result = login(config, http=_token_client(handler), now=1000.0, sleep=lambda s: None)
    assert result.access_token == "tok"
    assert calls["token"] == 2


def test_login_access_denied_raises_immediately(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        return httpx.Response(400, json={"error": "access_denied",
                                         "error_description": "user declined"})

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="access_denied"):
        login(config, http=_token_client(handler), now=1000.0, sleep=lambda s: None)


def test_login_expires_before_approval_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response(expires_in=3, interval=1))
        return httpx.Response(400, json={"error": "authorization_pending"})

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="expired"):
        login(config, http=_token_client(handler), now=1000.0, sleep=lambda s: None)


def test_login_missing_token_url_raises(tmp_path):
    config = _config(tmp_path, token_url="")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_device_response())

    with pytest.raises(SecSSOError, match="token_url"):
        login(config, http=_token_client(handler), sleep=lambda s: None)


# -- get_user_token: the file cache + auto-refresh in front of login ------------------


def test_get_user_token_cache_hit_makes_no_network_call(tmp_path):
    cache_path = tmp_path / "user-token.json"
    cache_path.write_text(json.dumps({
        "access_token": "cached-user-tok", "refresh_token": "r1",
        "expires_at": 10_000.0, "sub": "", "email": "",
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called on a cache hit")

    config = _config(tmp_path)
    token = get_user_token(config, http=_token_client(handler), now=1000.0)
    assert token == "cached-user-tok"


def test_get_user_token_no_cache_raises_telling_user_to_login(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="secagent login"):
        get_user_token(config, now=1000.0)


def test_get_user_token_refreshes_when_within_expiry_buffer(tmp_path):
    cache_path = tmp_path / "user-token.json"
    # now=1000, expires_at=1030 -> only 30s left, buffer is 60s -> must refresh.
    cache_path.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "refresh-abc",
        "expires_at": 1030.0, "sub": "u", "email": "e",
    }))
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 300})

    config = _config(tmp_path)
    token = get_user_token(config, http=_token_client(handler), now=1000.0)

    assert token == "refreshed"
    assert captured["body"]["grant_type"] == ["refresh_token"]
    assert captured["body"]["refresh_token"] == ["refresh-abc"]
    assert captured["body"]["client_id"] == ["secagent-pi"]
    assert "client_secret" not in captured["body"]  # public client — no secret ever sent
    data = json.loads(cache_path.read_text())
    assert data["access_token"] == "refreshed"


def test_get_user_token_refresh_keeps_old_refresh_token_if_none_reissued(tmp_path):
    cache_path = tmp_path / "user-token.json"
    cache_path.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "keep-me", "expires_at": 1030.0,
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 300})
        # no refresh_token in the response

    config = _config(tmp_path)
    get_user_token(config, http=_token_client(handler), now=1000.0)
    data = json.loads(cache_path.read_text())
    assert data["refresh_token"] == "keep-me"


def test_get_user_token_expired_without_refresh_token_raises(tmp_path):
    cache_path = tmp_path / "user-token.json"
    cache_path.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "", "expires_at": 1030.0,
    }))
    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="secagent login"):
        get_user_token(config, now=1000.0)


def test_get_user_token_refresh_failure_raises_and_mentions_login(tmp_path):
    cache_path = tmp_path / "user-token.json"
    cache_path.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "dead-refresh", "expires_at": 1030.0,
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    config = _config(tmp_path)
    with pytest.raises(SecSSOError, match="secagent login"):
        get_user_token(config, http=_token_client(handler), now=1000.0)
    # A failed refresh must not corrupt/clear the existing cache file.
    assert json.loads(cache_path.read_text())["access_token"] == "stale"


# -- peek_user_token: read-only, no side effects --------------------------------------


def test_peek_user_token_returns_none_when_absent(tmp_path):
    config = _config(tmp_path)
    assert peek_user_token(config) is None


def test_peek_user_token_returns_none_on_corrupt_cache(tmp_path):
    (tmp_path / "user-token.json").write_text("{ not valid")
    config = _config(tmp_path)
    assert peek_user_token(config) is None


def test_peek_user_token_reads_cached_identity(tmp_path):
    (tmp_path / "user-token.json").write_text(json.dumps({
        "access_token": "tok", "refresh_token": "r", "expires_at": 5000.0,
        "sub": "user-1", "email": "u1@example.com",
    }))
    config = _config(tmp_path)
    cached = peek_user_token(config)
    assert cached is not None
    assert cached.access_token == "tok"
    assert cached.sub == "user-1"
    assert cached.email == "u1@example.com"


# -- refresh_user_token (direct) -------------------------------------------------------


def test_refresh_user_token_missing_token_url_raises(tmp_path):
    config = _config(tmp_path, token_url="")
    with pytest.raises(SecSSOError, match="token_url"):
        refresh_user_token(config, "some-refresh-token")


# -- logout -----------------------------------------------------------------------------


def test_logout_removes_existing_cache(tmp_path):
    cache_path = tmp_path / "user-token.json"
    cache_path.write_text(json.dumps({
        "access_token": "tok", "refresh_token": "r", "expires_at": 5000.0,
    }))
    config = _config(tmp_path)
    assert logout(config) is True
    assert not cache_path.exists()


def test_logout_is_idempotent_when_nothing_cached(tmp_path):
    config = _config(tmp_path)
    assert logout(config) is False
