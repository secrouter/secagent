"""`secagent doctor` must verify what it claims.

The endpoint probe previously only listed models, which cannot detect the two failure
modes that actually bite: a wrong/evicted model name (every real call 404s) and a
reasoning model whose output budget is consumed before it emits any content. Both are
silent — callers read an empty completion as "nothing to report".
"""

from __future__ import annotations

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
