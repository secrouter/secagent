"""LeanCTX lockdown module — config.toml + env generation + the non-fatal compress bridge.

See :mod:`secagent.leanctx`. These lock in the CMMC/air-gapped posture the generated config +
env must always carry, and the contract that compressing secagent's own calls can NEVER drop a
request when LeanCTX is absent or down.
"""

from __future__ import annotations

import json

import httpx

from secagent import leanctx
from secagent.config import LeanCtxConfig, LLMConfig
from secagent.llm import client as client_mod
from secagent.llm.client import LLMClient


def _chat_response(content: str = "ok") -> dict:
    return {
        "id": "x", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _client(handler, leanctx_cfg=None) -> LLMClient:
    cfg = LLMConfig(base_url="http://mock/v1", max_retries=1)
    http = httpx.Client(base_url=cfg.base_url, transport=httpx.MockTransport(handler))
    return LLMClient(cfg, http=http, leanctx=leanctx_cfg)


def test_config_toml_is_locked_down_and_cache_aware():
    toml = leanctx.config_toml(LeanCtxConfig())
    assert 'rules_injection = "off"' in toml
    assert 'tool_profile = "minimal"' in toml
    assert "proxy_enabled = true" in toml
    assert "proxy_port = 4444" in toml               # from the default 127.0.0.1:4444 endpoint
    assert 'history_mode = "cache-aware"' in toml     # SecRouter/SecLLM prompt-cache safe
    assert "[memory]" in toml and "enabled = false" in toml   # persistence off by default


def test_config_toml_port_follows_endpoint():
    assert "proxy_port = 5599" in leanctx.config_toml(LeanCtxConfig(endpoint="http://127.0.0.1:5599"))


def test_config_toml_persist_on_omits_memory_off():
    assert "enabled = false" not in leanctx.config_toml(LeanCtxConfig(persist_context=True))


def test_lockdown_env_enforces_airgapped_posture():
    env = leanctx.lockdown_env(LeanCtxConfig())
    assert env["LEAN_CTX_NO_UPDATE_CHECK"] == "1"     # no update phone-home
    assert env["LEAN_CTX_HARDEN"] == "1"
    assert env["LEAN_CTX_TELEMETRY"] == "0"
    assert env["LEAN_CTX_PROXY_HISTORY_MODE"] == "cache-aware"
    assert env["LEAN_CTX_PI_MODE"] == "additive"
    assert env["LEAN_CTX_PI_ENABLE_MCP"] == "0"
    assert env["LEAN_CTX_NO_PERSIST"] == "1"          # no CUI at rest by default


def test_lockdown_env_respects_opt_ins():
    env = leanctx.lockdown_env(LeanCtxConfig(
        persist_context=True, harden=False, no_update_check=False,
        pi_enable_mcp=True, pi_mode="replace"))
    assert "LEAN_CTX_NO_PERSIST" not in env           # persistence opted in
    assert "LEAN_CTX_HARDEN" not in env
    assert "LEAN_CTX_NO_UPDATE_CHECK" not in env
    assert env["LEAN_CTX_PI_ENABLE_MCP"] == "1"
    assert env["LEAN_CTX_PI_MODE"] == "replace"


def test_compress_messages_passthrough_when_disabled():
    msgs = [{"role": "user", "content": "hi"}]
    assert leanctx.compress_messages(LeanCtxConfig(enabled=False), msgs, model="m") is msgs
    assert leanctx.compress_messages(LeanCtxConfig(compress_own_calls=False), msgs, model="m") is msgs
    assert leanctx.compress_messages(LeanCtxConfig(), [], model="m") == []


def test_compress_messages_passthrough_when_daemon_absent():
    # enabled + compress_own_calls on (defaults), but no SDK/daemon reachable → original returned:
    # a compression outage must never drop or corrupt a governed request.
    msgs = [{"role": "user", "content": "hi"}]
    assert leanctx.compress_messages(LeanCtxConfig(endpoint="http://127.0.0.1:1"), msgs, model="m") == msgs


# ── LLMClient wiring: the governed conversational path (review/chat) compresses ──────────────
def test_client_compresses_when_leanctx_configured(monkeypatch):
    seen: dict = {}

    def spy(cfg, messages, *, model):
        seen.update(cfg=cfg, messages=messages, model=model)
        return [{"role": "user", "content": "COMPRESSED"}]

    monkeypatch.setattr(client_mod, "compress_messages", spy)
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response())

    c = _client(handler, leanctx_cfg=LeanCtxConfig())
    c.chat([{"role": "user", "content": "hello"}])
    assert seen["messages"] == [{"role": "user", "content": "hello"}]   # got the originals
    assert seen["model"] == c.config.model
    assert posted["body"]["messages"] == [{"role": "user", "content": "COMPRESSED"}]  # used the result


def test_client_does_not_compress_without_leanctx(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(client_mod, "compress_messages",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or a[1])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response())

    _client(handler, leanctx_cfg=None).chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 0   # leanctx=None → compressor never invoked (tuned paths unaffected)


# ── wire_pi (best-effort pi registration) ────────────────────────────────────────────────────
def test_wire_pi_skips_when_binary_absent(monkeypatch):
    monkeypatch.setattr(leanctx, "binary_installed", lambda: False)
    steps = leanctx.wire_pi(LeanCtxConfig())
    assert len(steps) == 1 and "not found" in steps[0]   # non-fatal skip


def test_wire_pi_runs_init_and_harden_with_lockdown_env(monkeypatch):
    monkeypatch.setattr(leanctx, "binary_installed", lambda: True)
    calls: list = []

    class _R:
        returncode = 0

    def runner(argv, env):
        calls.append((argv, env))
        return _R()

    steps = leanctx.wire_pi(LeanCtxConfig(), runner=runner)
    assert calls[0][0] == ["lean-ctx", "init", "--agent", "pi"]
    assert calls[1][0] == ["lean-ctx", "harden"]
    assert calls[0][1]["LEAN_CTX_NO_UPDATE_CHECK"] == "1"   # lockdown env carried into the CLI
    assert calls[0][1]["LEAN_CTX_HARDEN"] == "1"
    assert len(steps) == 2


# ── onboarding: run_init writes the locked-down config.toml ───────────────────────────────────
def test_run_init_writes_locked_down_leanctx_config(tmp_path, monkeypatch):
    import stat

    from secagent import leanctx as leanctx_mod
    from secagent.onboarding import run_init

    monkeypatch.setattr(leanctx_mod, "binary_installed", lambda: False)  # hermetic: no real CLI
    lc_toml = tmp_path / "lean-ctx.toml"
    res = run_init(
        domain="test.internal",
        models_json_path=tmp_path / "models.json",
        config_path=tmp_path / "config.yaml",
        leanctx=LeanCtxConfig(),
        leanctx_config_path=lc_toml,
    )
    assert res.leanctx_config_path == lc_toml
    body = lc_toml.read_text()
    assert 'rules_injection = "off"' in body and 'history_mode = "cache-aware"' in body
    assert stat.S_IMODE(lc_toml.stat().st_mode) == 0o600       # 0600 (config inside the boundary)
    assert res.leanctx_steps and "not found" in res.leanctx_steps[0]
    assert any("LeanCTX" in ln for ln in res.summary_lines())  # surfaced in the CLI report


def test_run_init_skips_leanctx_when_disabled(tmp_path):
    from secagent.onboarding import run_init

    res = run_init(
        domain="test.internal",
        models_json_path=tmp_path / "models.json",
        config_path=tmp_path / "config.yaml",
        leanctx=LeanCtxConfig(enabled=False),
        leanctx_config_path=tmp_path / "lean-ctx.toml",
    )
    assert res.leanctx_config_path is None
    assert not (tmp_path / "lean-ctx.toml").exists()
    assert res.leanctx_steps == []
