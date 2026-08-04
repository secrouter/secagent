"""`secagent doctor` must verify what it claims.

The endpoint probe previously only listed models, which cannot detect the two failure
modes that actually bite: a wrong/evicted model name (every real call 404s) and a
reasoning model whose output budget is consumed before it emits any content. Both are
silent — callers read an empty completion as "nothing to report".
"""

from __future__ import annotations

import collections
import json
import stat as stat_module
import time

import httpx

from secagent import doctor
from secagent.config import Settings


def _settings(model: str = "test-model") -> Settings:
    s = Settings()
    s.llm.base_url = "http://llm.invalid/v1"
    s.llm.model = model
    return s


def _post(monkeypatch, response: httpx.Response) -> dict:
    """Stub httpx.post, capturing the request kwargs for assertions."""
    seen: dict = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen.update(kw)
        return response

    monkeypatch.setattr(doctor.httpx, "post", fake_post)
    return seen


def _completion(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish}]
    })


def test_probe_off_does_not_call_the_network(monkeypatch):
    def explode(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("probe=False must not hit the network")

    monkeypatch.setattr(doctor.httpx, "post", explode)
    c = doctor.check_llm_endpoint(_settings(), probe=False)
    assert c.ok and "configured" in c.detail


def test_probe_sends_a_real_completion(monkeypatch):
    seen = _post(monkeypatch, _completion("READY"))
    c = doctor.check_llm_endpoint(_settings("gemma-x"), probe=True)
    assert c.ok and "generated ok" in c.detail
    # It must POST to chat/completions with the CONFIGURED model — listing models is
    # exactly what failed to catch a bad model name.
    assert seen["url"].endswith("/chat/completions")
    assert seen["json"]["model"] == "gemma-x"


def test_probe_fails_on_unknown_model(monkeypatch):
    """Regression: a model that isn't loaded returns 4xx on completions while /models
    still returns 200. The old probe reported OK; this must fail."""
    _post(monkeypatch, httpx.Response(
        404, json={"error": {"message": 'Invalid model identifier "nope"'}}))
    c = doctor.check_llm_endpoint(_settings("nope"), probe=True)
    assert not c.ok and c.severity == "error"
    assert "rejected" in c.detail and "nope" in c.detail


def test_probe_fails_on_empty_content_from_reasoning_model(monkeypatch):
    """A reasoning model can 200 with empty content when the budget went to reasoning."""
    _post(monkeypatch, _completion("", finish="length"))
    c = doctor.check_llm_endpoint(_settings(), probe=True)
    assert not c.ok and c.severity == "error"
    assert "EMPTY content" in c.detail
    assert "max_output_tokens" in c.detail  # actionable hint


def test_probe_fails_on_unrecognized_shape(monkeypatch):
    _post(monkeypatch, httpx.Response(200, json={"unexpected": True}))
    c = doctor.check_llm_endpoint(_settings(), probe=True)
    assert not c.ok and "unrecognized response shape" in c.detail


def test_probe_reports_unreachable(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(doctor.httpx, "post", boom)
    c = doctor.check_llm_endpoint(_settings(), probe=True)
    assert not c.ok and "unreachable" in c.detail


def test_analysis_backends_check_names_missing_extras(monkeypatch):
    """A missing backend is a real capability loss (empty call map) and must be named."""
    monkeypatch.setattr(doctor, "run_doctor", doctor.run_doctor)  # keep module import
    import secagent.affordances.clang_ast as ca
    import secagent.affordances.csharp_ast as cs
    import secagent.affordances.rust_ast as rs

    monkeypatch.setattr(ca, "clang_available", lambda: False)
    monkeypatch.setattr(cs, "csharp_available", lambda: True)
    monkeypatch.setattr(rs, "rust_available", lambda: True)

    c = doctor.check_analysis_backends(_settings())
    assert c.ok and c.severity == "warn"          # degraded, not fatal
    assert "libclang" in c.detail and "secagent[clang]" in c.detail
    assert "tree-sitter C#" in c.detail            # present ones still listed


def test_analysis_backends_all_present(monkeypatch):
    import secagent.affordances.clang_ast as ca
    import secagent.affordances.csharp_ast as cs
    import secagent.affordances.rust_ast as rs

    for mod, name in ((ca, "clang_available"), (cs, "csharp_available"), (rs, "rust_available")):
        monkeypatch.setattr(mod, name, lambda: True)
    c = doctor.check_analysis_backends(_settings())
    assert c.ok and c.severity != "warn" and "all present" in c.detail


def test_run_doctor_includes_the_new_checks():
    names = {c.name for c in doctor.run_doctor(_settings(), probe_endpoint=False)}
    assert {"analysis_backends", "llm_endpoint"} <= names


# -- heavy (container image) analysis backends -------------------------------------
#
# `check_analysis_backends` only ever probed the light backends (libclang, tree-sitter).
# An evaluation run had both the Roslyn and rust-analyzer images built and working, and
# doctor said nothing; a user with neither gets the same clean bill of health and then
# wonders why `analyze deep` does nothing. `check_heavy_analysis_backends` probes with
# `heavy.image_available` — the exact function `heavy.py` itself uses to decide whether
# to run — so "present" here means a heavy run could actually start.


def test_heavy_backends_reports_present(monkeypatch):
    """The attack: assert it reports *present* correctly, not faked — patch the real
    seam (`heavy.image_available`) and check both the image names and severity."""
    import secagent.affordances.heavy as heavy

    monkeypatch.setattr(heavy, "image_available", lambda runtime, image: True)
    s = _settings()
    c = doctor.check_heavy_analysis_backends(s)
    assert c.ok and c.severity != "warn"
    assert "all present" in c.detail
    assert s.affordances.analyzer_image_dotnet in c.detail
    assert s.affordances.analyzer_image_rust in c.detail


def test_heavy_backends_reports_absent(monkeypatch):
    """The failure mode that matters: a false OK. Both images missing must warn and name
    the real build command (`heavy.py`'s own log lines), not an invented one."""
    import secagent.affordances.heavy as heavy

    monkeypatch.setattr(heavy, "image_available", lambda runtime, image: False)
    s = _settings()
    c = doctor.check_heavy_analysis_backends(s)
    assert c.ok and c.severity == "warn"          # capability loss, not fatal
    assert "missing" in c.detail
    assert "make analyzer-dotnet" in c.detail
    assert "make analyzer-rust" in c.detail
    assert s.affordances.analyzer_image_dotnet in c.detail
    assert s.affordances.analyzer_image_rust in c.detail


def test_heavy_backends_reports_partial(monkeypatch):
    """One present, one absent: both directions must show up in the SAME check, since a
    real evaluation host can have one image built and not the other."""
    import secagent.affordances.heavy as heavy

    def fake(runtime, image):
        return image == "secagent-analyzer-dotnet:latest"

    monkeypatch.setattr(heavy, "image_available", fake)
    s = _settings()
    c = doctor.check_heavy_analysis_backends(s)
    assert c.ok and c.severity == "warn"
    assert "secagent-analyzer-dotnet:latest" in c.detail
    assert "secagent-analyzer-rust:latest" in c.detail
    assert "make analyzer-rust" in c.detail        # only the missing one names its build
    assert "make analyzer-dotnet" not in c.detail  # present one must not also warn-hint


def test_heavy_backends_probes_the_real_seam(monkeypatch):
    """Regression guard for the exact bug: a check that never calls `image_available`
    would report OK from nothing. Assert the seam is actually invoked."""
    import secagent.affordances.heavy as heavy

    calls: list[tuple[str, str]] = []

    def fake(runtime, image):
        calls.append((runtime, image))
        return False

    monkeypatch.setattr(heavy, "image_available", fake)
    doctor.check_heavy_analysis_backends(_settings())
    assert len(calls) == 2
    assert all(runtime == "docker" for runtime, _ in calls)


def test_run_doctor_includes_heavy_backends():
    names = {c.name for c in doctor.run_doctor(_settings(), probe_endpoint=False)}
    assert "heavy_analysis_backends" in names


# -- llm_endpoint: `--probe` must RESOLVE llm.api_key, not send it verbatim ----------
#
# `llm.api_key` may be a "!command"/"$VAR" (secretval.py) -- notably "!secagent token
# --user", the per-user default `secagent init` writes. Sending that literal string
# as a bearer credential is a real generation failure, not a hypothetical one.


def test_probe_resolves_command_api_key(monkeypatch):
    seen = _post(monkeypatch, _completion("READY"))
    s = _settings()
    s.llm.api_key = "!echo resolved-key-value"
    c = doctor.check_llm_endpoint(s, probe=True)
    assert c.ok
    assert seen["headers"]["Authorization"] == "Bearer resolved-key-value"


def test_probe_still_sends_a_plain_literal_api_key_unchanged(monkeypatch):
    seen = _post(monkeypatch, _completion("READY"))
    s = _settings()
    s.llm.api_key = "not-needed"
    c = doctor.check_llm_endpoint(s, probe=True)
    assert c.ok
    assert seen["headers"]["Authorization"] == "Bearer not-needed"


def test_probe_reports_error_when_api_key_unresolvable(monkeypatch):
    monkeypatch.delenv("SECAGENT_TEST_UNDEFINED_VAR_XYZ", raising=False)
    s = _settings()
    s.llm.api_key = "$SECAGENT_TEST_UNDEFINED_VAR_XYZ"
    c = doctor.check_llm_endpoint(s, probe=True)
    assert not c.ok and c.severity == "error"
    assert "llm.api_key" in c.detail


# -- developer onboarding checks (install.sh / secagent init / secagent login) ------


def test_python_version_check_passes_on_current_interpreter():
    c = doctor.check_python_version()
    assert c.ok and c.severity == "info"
    assert "Python" in c.detail


def test_python_version_check_fails_below_3_11(monkeypatch):
    fake = collections.namedtuple("V", ["major", "minor", "micro", "releaselevel", "serial"])
    monkeypatch.setattr(doctor.sys, "version_info", fake(3, 9, 6, "final", 0))
    c = doctor.check_python_version()
    assert not c.ok and c.severity == "error"
    assert "3.9" in c.detail
    assert ">=3.11" in c.detail


def test_secagent_version_check():
    c = doctor.check_secagent_version()
    assert c.ok and c.severity == "info"
    assert "secagent" in c.detail


def test_pi_runtime_missing_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_pi_runtime()
    assert c.ok and c.severity == "warn"
    assert "pi not found" in c.detail


def test_pi_runtime_present_reports_version(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda name: "/usr/local/bin/pi" if name == "pi" else None)
    monkeypatch.setattr(doctor, "_binary_version", lambda binary, flag: "pi 0.83.0")
    c = doctor.check_pi_runtime()
    assert c.ok and c.severity == "info"
    assert "pi 0.83.0" in c.detail


def test_pi_runtime_present_but_version_unknown_still_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda name: "/usr/local/bin/pi" if name == "pi" else None)
    monkeypatch.setattr(doctor, "_binary_version", lambda binary, flag: "")
    c = doctor.check_pi_runtime()
    assert c.ok and c.severity == "info"
    assert "/usr/local/bin/pi" in c.detail


def test_node_runtime_missing_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_node_runtime()
    assert c.ok and c.severity == "warn"
    assert "node" in c.detail


def test_node_runtime_present(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("node", "npm") else None)
    c = doctor.check_node_runtime()
    assert c.ok and c.severity == "info"
    assert "node:" in c.detail and "npm:" in c.detail


def test_binary_version_best_effort_empty_on_failure(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(doctor.subprocess, "run", boom)
    assert doctor._binary_version("nonexistent-binary", "--version") == ""


def test_onboarding_check_reports_not_done(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c = doctor.check_onboarding()
    assert c.ok and c.severity == "warn"
    assert "not done" in c.detail
    assert "secagent init" in c.detail


def test_onboarding_check_reports_done(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    (tmp_path / ".pi" / "agent" / "models.json").write_text("{}")
    (tmp_path / ".secagent").mkdir(parents=True)
    (tmp_path / ".secagent" / "config.yaml").write_text("llm: {}\n")
    c = doctor.check_onboarding()
    assert c.ok and c.severity == "info"
    assert "done" in c.detail


def _with_user_cache_path(tmp_path) -> Settings:
    s = _settings()
    s.secsso.user_token_cache_path = str(tmp_path / "user-token.json")
    return s


def test_user_login_check_not_logged_in(tmp_path):
    c = doctor.check_user_login(_with_user_cache_path(tmp_path))
    assert c.ok and c.severity == "warn"
    assert "not logged in" in c.detail
    assert "secagent login" in c.detail


def test_user_login_check_expired(tmp_path):
    s = _with_user_cache_path(tmp_path)
    (tmp_path / "user-token.json").write_text(json.dumps({
        "access_token": "x", "refresh_token": "", "expires_at": 1.0, "sub": "", "email": "",
    }))
    c = doctor.check_user_login(s)
    assert c.ok and c.severity == "warn"
    assert "EXPIRED" in c.detail


def test_user_login_check_valid_shows_identity_and_remaining_time(tmp_path):
    s = _with_user_cache_path(tmp_path)
    future = time.time() + 3600
    (tmp_path / "user-token.json").write_text(json.dumps({
        "access_token": "x", "refresh_token": "r", "expires_at": future,
        "sub": "user-1", "email": "u@example.com",
    }))
    c = doctor.check_user_login(s)
    assert c.ok and c.severity == "info"
    assert "user-1" in c.detail


def test_fix_permissions_creates_and_hardens_dirs(tmp_path):
    s = _settings()
    s.secsso.token_cache_path = str(tmp_path / "svc" / "secsso-token.json")
    s.secsso.user_token_cache_path = str(tmp_path / "usr" / "user-token.json")
    dirs = doctor.fix_permissions(s)
    assert (tmp_path / "svc").is_dir()
    assert (tmp_path / "usr").is_dir()
    assert stat_module.S_IMODE((tmp_path / "svc").stat().st_mode) == 0o700
    assert stat_module.S_IMODE((tmp_path / "usr").stat().st_mode) == 0o700
    assert dirs == sorted({tmp_path / "svc", tmp_path / "usr"})


def test_fix_permissions_dedupes_shared_parent_dir(tmp_path):
    shared = tmp_path / "auth"
    s = _settings()
    s.secsso.token_cache_path = str(shared / "secsso-token.json")
    s.secsso.user_token_cache_path = str(shared / "user-token.json")
    dirs = doctor.fix_permissions(s)
    assert dirs == [shared]


def test_secrouter_models_skipped_without_probe():
    c = doctor.check_secrouter_models_endpoint(_settings(), probe=False)
    assert c.ok and "skipped" in c.detail


def test_secrouter_models_reachable_and_authenticated(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers")
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    s = _settings()
    s.llm.base_url = "https://secrouter.example.com:47002/v1"
    s.llm.api_key = "literal-key"
    c = doctor.check_secrouter_models_endpoint(s, probe=True)
    assert c.ok and c.severity == "info"
    assert seen["url"] == "https://secrouter.example.com:47002/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer literal-key"
    assert "HTTP 200" in c.detail


def test_secrouter_models_unresolvable_api_key_still_probes_unauthenticated(monkeypatch):
    """This check must work BEFORE `secagent login` has cached anything -- unlike
    `check_llm_endpoint`, an unresolvable api_key is not fatal here."""
    def fake_get(url, **kw):
        assert kw.get("headers") == {}
        return httpx.Response(401, json={})

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    monkeypatch.delenv("SECAGENT_TEST_UNDEFINED_VAR_XYZ", raising=False)
    s = _settings()
    s.llm.api_key = "$SECAGENT_TEST_UNDEFINED_VAR_XYZ"
    c = doctor.check_secrouter_models_endpoint(s, probe=True)
    assert c.ok  # still reachable (HTTP 401), just unauthenticated
    assert "HTTP 401" in c.detail


def test_secrouter_models_unreachable_warns(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(doctor.httpx, "get", boom)
    c = doctor.check_secrouter_models_endpoint(_settings(), probe=True)
    assert not c.ok and c.severity == "warn"
    assert "unreachable" in c.detail


def test_secsso_device_endpoints_not_configured():
    c = doctor.check_secsso_device_endpoints(_settings(), probe=True)
    assert c.ok and c.severity == "info"
    assert "not configured" in c.detail


def test_secsso_device_endpoints_configured_without_probe_makes_no_network_call(monkeypatch):
    def explode(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("probe=False must not hit the network")

    monkeypatch.setattr(doctor.httpx, "get", explode)
    s = _settings()
    s.secsso.device_authorization_url = "https://secsso.example.com/application/o/device/"
    s.secsso.token_url = "https://secsso.example.com/application/o/token/"
    c = doctor.check_secsso_device_endpoints(s, probe=False)
    assert c.ok and c.severity == "info"
    assert "device=" in c.detail and "token=" in c.detail


def test_secsso_device_endpoints_reachable(monkeypatch):
    monkeypatch.setattr(doctor.httpx, "get", lambda url, **kw: httpx.Response(405))
    s = _settings()
    s.secsso.device_authorization_url = "https://secsso.example.com/application/o/device/"
    s.secsso.token_url = "https://secsso.example.com/application/o/token/"
    c = doctor.check_secsso_device_endpoints(s, probe=True)
    assert c.ok and c.severity == "info"
    assert "reachable" in c.detail


def test_secsso_device_endpoints_unreachable_warns(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(doctor.httpx, "get", boom)
    s = _settings()
    s.secsso.device_authorization_url = "https://secsso.example.com/application/o/device/"
    s.secsso.token_url = "https://secsso.example.com/application/o/token/"
    c = doctor.check_secsso_device_endpoints(s, probe=True)
    assert not c.ok and c.severity == "warn"
    assert "unreachable" in c.detail
    assert "device_authorization_url" in c.detail


def test_run_doctor_includes_onboarding_checks():
    names = {c.name for c in doctor.run_doctor(_settings(), probe_endpoint=False)}
    assert {
        "python_version", "secagent_version", "pi_runtime", "node_runtime",
        "onboarding", "user_login", "secrouter_models", "secsso_device_endpoints",
    } <= names
