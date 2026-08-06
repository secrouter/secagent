"""LeanCtxConfig — the locked-down LeanCTX context-compression config.

Guards the CMMC/air-gapped defaults (on, loopback-only, no persistence, no phone-home,
hardened, prompt-cache-safe, version-pinned) and that the section loads/overrides like every
other secagent config section. See :class:`secagent.config.LeanCtxConfig` + docs/leanctx.md.
"""

from __future__ import annotations

import textwrap

from secagent.config import LeanCtxConfig, Settings, load_settings


def test_defaults_are_on_and_locked_down():
    lc = Settings().leanctx
    assert lc.enabled is True                      # on by default (wired into pi + secagent)
    assert lc.is_loopback                           # 127.0.0.1 — LeanCTX sees CUI, stays local
    assert lc.persist_context is False              # no CUI written to disk by default
    assert lc.no_update_check is True               # air-gapped: no update phone-home
    assert lc.harden is True                         # `lean-ctx harden`
    assert lc.telemetry is False
    assert lc.proxy_history_mode == "cache-aware"    # keeps SecRouter/SecLLM prompt cache hitting
    assert lc.pi_mode == "additive"
    assert lc.pi_enable_mcp is False
    assert lc.version and lc.client_version          # pinned, never floating


def test_is_loopback_flags_routable_endpoint():
    for good in ("http://127.0.0.1:4444", "http://localhost:4444", "http://[::1]:4444"):
        assert LeanCtxConfig(endpoint=good).is_loopback, good
    for bad in ("http://10.0.0.5:4444", "https://leanctx.internal:4444"):
        assert not LeanCtxConfig(endpoint=bad).is_loopback, bad


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))        # isolate the ~/.secagent/config.yaml layer
    monkeypatch.setenv("SECAGENT_LEANCTX__ENABLED", "false")
    monkeypatch.setenv("SECAGENT_LEANCTX__PI_MODE", "replace")
    s = load_settings()
    assert s.leanctx.enabled is False
    assert s.leanctx.pi_mode == "replace"
    assert s.llm.base_url == Settings().llm.base_url  # unrelated sections untouched


def test_yaml_section_is_a_known_section(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "c.yaml"
    cfg.write_text(textwrap.dedent("""
        leanctx:
          persist_context: true
          endpoint: http://127.0.0.1:9999
    """))
    s = load_settings(config_path=str(cfg))          # would raise "unknown section" if unregistered
    assert s.leanctx.persist_context is True
    assert s.leanctx.endpoint == "http://127.0.0.1:9999"


def test_present_in_safe_dict_with_no_secrets():
    data = Settings().safe_dict()
    assert "leanctx" in data                          # registered in Settings + pristine dump
    assert data["leanctx"]["enabled"] is True         # no secret fields → nothing redacted
