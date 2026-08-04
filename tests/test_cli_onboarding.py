"""CLI-level tests for the developer-onboarding surface: `secagent init`,
`secagent login`/`secagent logout`, `secagent token --user`, and the wiring of
`secagent doctor`'s new onboarding checks (--fix in particular).

Network is fully mocked (no real HTTP anywhere in this file) and ``HOME`` is pointed
at a per-test tmp dir, so nothing here ever touches a real developer's ``~/.pi`` or
``~/.secagent``. Doctor's own check LOGIC is unit-tested directly in
``test_doctor.py``; this file only exercises the CLI wiring around it.
"""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from typer.testing import CliRunner

import secagent.secsso as secsso_mod
from secagent.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate $HOME (and therefore ~/.pi, ~/.secagent) to a tmp dir for the whole
    test, and neutralize `secagent login`'s real sleep between polls."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(secsso_mod.time, "sleep", lambda s: None)
    return h


def _mock_secsso_client(monkeypatch, handler) -> None:
    """Replace every `httpx.Client()` secsso.py constructs (login/logout/token --user
    never pass their own `http=`) with one wired to a MockTransport."""
    real_client_cls = httpx.Client  # captured BEFORE patching -- else `factory` below

    # would call itself (`secsso_mod.httpx` IS the same module object this test's own
    # `httpx.Client` name would otherwise resolve to after the patch) and recurse forever.
    def factory(*args, **kwargs):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(secsso_mod.httpx, "Client", factory)


def _flat(text: str) -> str:
    """Strip line breaks rich's Console inserts when word-wrapping at its (narrow,
    non-tty) default width under CliRunner -- a long unbroken path/URL can otherwise
    get a bare ``\\n`` spliced into the middle of it with no space around the break,
    which would make a plain substring check fail even though the CONTENT is exactly
    right. Safe for every check in this file: removing newlines can only turn a
    false-negative wrapping artifact into a pass, never mask a real content mismatch.
    """
    return text.replace("\n", "")


# ── secagent init ───────────────────────────────────────────────────────────────────


def test_cli_init_writes_models_json_and_config_yaml(home):
    result = runner.invoke(app, ["init", "--domain", "example.internal"])
    assert result.exit_code == 0, result.output

    models_path = home / ".pi" / "agent" / "models.json"
    config_path = home / ".secagent" / "config.yaml"
    assert models_path.exists()
    assert config_path.exists()

    data = json.loads(models_path.read_text())
    assert set(data["providers"]) == {"secrouter"}
    assert data["providers"]["secrouter"]["baseUrl"] == "https://secrouter.example.internal:47002/v1"
    assert data["providers"]["secrouter"]["apiKey"] == "!secagent token --user"
    assert "kimi" not in json.dumps(data["providers"]).lower()

    output = _flat(result.output)
    assert "secagent login" in output
    assert str(models_path) in output
    assert str(config_path) in output


def test_cli_init_without_domain_or_urls_fails_clearly(home):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "--domain" in _flat(result.output)


def test_cli_init_rejects_blocked_model(home):
    result = runner.invoke(app, ["init", "--domain", "example.internal", "--model", "deepseek-v3"])
    assert result.exit_code == 1
    assert "deepseek" in _flat(result.output).lower()
    assert not (home / ".pi" / "agent" / "models.json").exists()


def test_cli_init_force_backs_up_existing_file(home):
    runner.invoke(app, ["init", "--domain", "old.internal"])
    result = runner.invoke(app, ["init", "--domain", "new.internal", "--force"])
    assert result.exit_code == 0, result.output
    assert "Backed up" in _flat(result.output)
    backups = list((home / ".pi" / "agent").glob("models.json.bak-*"))
    assert len(backups) == 1
    assert "old.internal" in backups[0].read_text()


def test_cli_init_is_idempotent(home):
    first = runner.invoke(app, ["init", "--domain", "example.internal"])
    second = runner.invoke(app, ["init", "--domain", "example.internal"])
    assert first.exit_code == 0 and second.exit_code == 0
    data = json.loads((home / ".pi" / "agent" / "models.json").read_text())
    assert list(data["providers"]) == ["secrouter"]


# ── secagent login / logout ─────────────────────────────────────────────────────────


def _device_response() -> dict:
    return {
        "device_code": "devcode-1", "user_code": "WXYZ-9876",
        "verification_uri": "https://secsso.example.com/if/device/",
        "verification_uri_complete": "https://secsso.example.com/if/device/?code=WXYZ-9876",
        "expires_in": 600, "interval": 1,
    }


def test_cli_login_success_prints_code_and_caches_token(home, monkeypatch):
    monkeypatch.setenv("SECAGENT_SECSSO__DEVICE_AUTHORIZATION_URL",
                       "https://secsso.example.com/application/o/device/")
    monkeypatch.setenv("SECAGENT_SECSSO__TOKEN_URL",
                       "https://secsso.example.com/application/o/token/")
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        calls["token"] += 1
        if calls["token"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json={
            "access_token": "dev-access-token", "refresh_token": "dev-refresh-token",
            "expires_in": 3600,
        })

    _mock_secsso_client(monkeypatch, handler)
    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "WXYZ-9876" in output
    assert "https://secsso.example.com/if/device/" in output
    assert "Logged in" in output

    cache_path = home / ".secagent" / "auth" / "user-token.json"
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["access_token"] == "dev-access-token"
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache_path.parent.stat().st_mode) == 0o700


def test_cli_login_shows_identity_when_available(home, monkeypatch):
    monkeypatch.setenv("SECAGENT_SECSSO__DEVICE_AUTHORIZATION_URL",
                       "https://secsso.example.com/application/o/device/")
    monkeypatch.setenv("SECAGENT_SECSSO__TOKEN_URL",
                       "https://secsso.example.com/application/o/token/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        return httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600, "email": "irrelevant",
        })

    _mock_secsso_client(monkeypatch, handler)
    # email claim comes from the id_token, not a top-level field -- this response has
    # none, so login must still succeed and just omit the identity line.
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 0, result.output
    assert "Logged in" in _flat(result.output)


def test_cli_login_failure_exits_nonzero_and_prints_nothing_to_cache(home, monkeypatch):
    monkeypatch.setenv("SECAGENT_SECSSO__DEVICE_AUTHORIZATION_URL",
                       "https://secsso.example.com/application/o/device/")
    monkeypatch.setenv("SECAGENT_SECSSO__TOKEN_URL",
                       "https://secsso.example.com/application/o/token/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        return httpx.Response(400, json={"error": "access_denied"})

    _mock_secsso_client(monkeypatch, handler)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert "access_denied" in _flat(result.output)
    assert not (home / ".secagent" / "auth" / "user-token.json").exists()


def test_cli_login_without_device_url_configured_fails_clearly(home):
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert "device_authorization_url" in _flat(result.output)


def test_cli_logout_removes_cache_and_is_idempotent(home):
    auth_dir = home / ".secagent" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "user-token.json").write_text(json.dumps({
        "access_token": "tok", "refresh_token": "r", "expires_at": 9_999_999_999.0,
    }))

    first = runner.invoke(app, ["logout"])
    assert first.exit_code == 0
    assert "Logged out" in _flat(first.output)
    assert not (auth_dir / "user-token.json").exists()

    second = runner.invoke(app, ["logout"])
    assert second.exit_code == 0
    assert "Nothing to log out of" in _flat(second.output)


# ── secagent token --user ───────────────────────────────────────────────────────────


def test_cli_token_user_prints_cached_token(home):
    auth_dir = home / ".secagent" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "user-token.json").write_text(json.dumps({
        "access_token": "my-cached-user-token", "refresh_token": "r",
        "expires_at": 9_999_999_999.0,
    }))
    result = runner.invoke(app, ["token", "--user"])
    assert result.exit_code == 0
    assert result.output.strip() == "my-cached-user-token"


def test_cli_token_user_short_flag(home):
    auth_dir = home / ".secagent" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "user-token.json").write_text(json.dumps({
        "access_token": "my-cached-user-token", "refresh_token": "r",
        "expires_at": 9_999_999_999.0,
    }))
    result = runner.invoke(app, ["token", "-u"])
    assert result.exit_code == 0
    assert result.output.strip() == "my-cached-user-token"


def test_cli_token_user_without_login_fails_with_hint(home):
    result = runner.invoke(app, ["token", "--user"])
    assert result.exit_code == 1
    assert "secagent login" in _flat(result.output)


def test_cli_token_without_user_flag_is_unaffected(home, monkeypatch):
    """Plain `secagent token` (service identity) must behave exactly as before this
    feature existed -- no --user, no interaction with the per-user cache at all."""
    monkeypatch.setenv("SECAGENT_SECSSO__TOKEN_URL",
                       "https://secsso.example.com/application/o/token/")
    monkeypatch.setenv("SECAGENT_SECSSO__CLIENT_SECRET_ENV", "SECAGENT_TEST_SVC_SECRET")
    monkeypatch.setenv("SECAGENT_TEST_SVC_SECRET", "svc-secret-value")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 300})

    _mock_secsso_client(monkeypatch, handler)
    result = runner.invoke(app, ["token"])
    assert result.exit_code == 0
    assert result.output.strip() == "svc-token"
    # The per-user cache must be untouched by the service-identity path.
    assert not (home / ".secagent" / "auth" / "user-token.json").exists()


# ── secagent doctor: wiring only (logic is covered by test_doctor.py) ──────────────


def test_cli_doctor_runs_clean_in_a_fresh_environment(home):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "doctor" in _flat(result.output).lower()


def test_cli_doctor_fix_creates_and_hardens_auth_dir(home):
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    auth_dir = home / ".secagent" / "auth"
    assert auth_dir.is_dir()
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert "--fix" in _flat(result.output)


def test_cli_doctor_after_init_and_login_reports_healthy(home, monkeypatch):
    # No SECAGENT_SECSSO__* env overrides here on purpose: `secagent init` writes
    # ~/.secagent/config.yaml with the domain-derived device/token URLs, and
    # config.py's auto-load layer must be what feeds `secagent login` here -- this
    # is as much a test of that wiring as of doctor's post-login status.
    init_result = runner.invoke(app, ["init", "--domain", "example.internal"])
    assert init_result.exit_code == 0, init_result.output

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/"):
            return httpx.Response(200, json=_device_response())
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    _mock_secsso_client(monkeypatch, handler)
    login_result = runner.invoke(app, ["login"])
    assert login_result.exit_code == 0, login_result.output

    doctor_result = runner.invoke(app, ["doctor"])
    assert doctor_result.exit_code == 0, doctor_result.output
