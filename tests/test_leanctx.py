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
    assert leanctx.compress_messages(
        LeanCtxConfig(compress_own_calls=False), msgs, model="m") is msgs
    assert leanctx.compress_messages(LeanCtxConfig(), [], model="m") == []


def test_compress_messages_passthrough_when_daemon_absent():
    # enabled + compress_own_calls on (defaults), but no SDK/daemon reachable → original returned:
    # a compression outage must never drop or corrupt a governed request.
    msgs = [{"role": "user", "content": "hi"}]
    assert leanctx.compress_messages(
        LeanCtxConfig(endpoint="http://127.0.0.1:1"), msgs, model="m") == msgs


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
    assert posted["body"]["messages"] == [{"role": "user", "content": "COMPRESSED"}]  # compressed


def test_client_does_not_compress_without_leanctx(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(client_mod, "compress_messages",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or a[1])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response())

    _client(handler, leanctx_cfg=None).chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 0   # leanctx=None → compressor never invoked (tuned paths unaffected)


# ── install_pi_extension (launch-time model: install, scope, NEVER harden the operator) ───────
def test_install_pi_extension_skips_when_binary_absent(monkeypatch):
    monkeypatch.setattr(leanctx, "binary_installed", lambda: False)
    steps = leanctx.install_pi_extension(LeanCtxConfig())
    assert len(steps) == 1 and "not found" in steps[0]   # non-fatal skip


def test_install_pi_extension_installs_but_never_hardens(monkeypatch):
    monkeypatch.setattr(leanctx, "binary_installed", lambda: True)
    # No global auto-load to remove (isolate the CLI call), so _deregister is a no-op here.
    monkeypatch.setattr(leanctx, "_deregister_global_pi_extension", lambda: [])
    calls: list = []

    class _R:
        returncode = 0

    def runner(argv, env):
        calls.append((argv, env))
        return _R()

    leanctx.install_pi_extension(LeanCtxConfig(), runner=runner)
    # ONE install call, and CRUCIALLY never `lean-ctx harden` (the operator-wrapping step).
    assert len(calls) == 1
    assert calls[0][0] == ["lean-ctx", "init", "--agent", "pi"]
    assert ["lean-ctx", "harden"] not in [c[0] for c in calls]
    assert calls[0][1]["LEAN_CTX_NO_UPDATE_CHECK"] == "1"   # lockdown env carried into the CLI


def test_install_pi_extension_deregisters_global_autoload(monkeypatch, tmp_path):
    monkeypatch.setattr(leanctx, "binary_installed", lambda: True)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"packages": ["npm:pi-lean-ctx", "npm:something-else"]}))
    rule = tmp_path / "lean-ctx.md"
    rule.write_text("rules")
    monkeypatch.setattr(leanctx, "PI_SETTINGS_JSON", settings)
    monkeypatch.setattr(leanctx, "PI_GLOBAL_RULE", rule)

    class _R:
        returncode = 0

    leanctx.install_pi_extension(LeanCtxConfig(), runner=lambda argv, env: _R())
    left = json.loads(settings.read_text())["packages"]
    assert "npm:pi-lean-ctx" not in left     # no longer auto-loaded by a bare pi
    assert "npm:something-else" in left       # unrelated entries untouched
    assert not rule.exists()                  # global rule drop removed


def test_wire_pi_is_back_compat_alias():
    assert leanctx.wire_pi is leanctx.install_pi_extension


# ── launch-time contract: pi_extension_entry / pi_launch_args / launch_pi ─────────────────────
def test_pi_extension_entry_honours_override_and_existence(tmp_path):
    ext = tmp_path / "index.ts"
    assert leanctx.pi_extension_entry({"SECAGENT_PI_LEANCTX_EXTENSION": str(ext)}) is None  # absent
    ext.write_text("// ext")
    assert leanctx.pi_extension_entry({"SECAGENT_PI_LEANCTX_EXTENSION": str(ext)}) == ext


def test_pi_launch_args_adds_e_flag_only_when_installed(tmp_path):
    ext = tmp_path / "index.ts"
    ext.write_text("// ext")
    env = {"SECAGENT_PI_LEANCTX_EXTENSION": str(ext)}
    assert leanctx.pi_launch_args(LeanCtxConfig(), env=env) == ["-e", str(ext)]
    assert leanctx.pi_launch_args(LeanCtxConfig(enabled=False), env=env) == []  # kill-switch
    missing = {"SECAGENT_PI_LEANCTX_EXTENSION": str(tmp_path / "nope.ts")}
    assert leanctx.pi_launch_args(LeanCtxConfig(), env=missing) == []            # not installed


def test_launch_pi_builds_argv_with_extension_and_lockdown_env(tmp_path, monkeypatch):
    ext = tmp_path / "index.ts"
    ext.write_text("// ext")
    monkeypatch.setenv("SECAGENT_PI_LEANCTX_EXTENSION", str(ext))
    captured: dict = {}

    def fake_exec(pi_bin, argv, env):
        captured.update(pi_bin=pi_bin, argv=argv, env=env)

    argv = leanctx.launch_pi(LeanCtxConfig(), ["--mode", "rpc"], exec_fn=fake_exec)
    assert argv == ["pi", "-e", str(ext), "--mode", "rpc"]
    assert captured["env"]["LEAN_CTX_HARDEN"] == "1"          # per-process lockdown, not host-wide
    assert captured["env"]["LEAN_CTX_NO_UPDATE_CHECK"] == "1"


def test_launch_pi_without_leanctx_still_runs_pi(tmp_path, monkeypatch):
    monkeypatch.delenv("SECAGENT_PI_LEANCTX_EXTENSION", raising=False)
    monkeypatch.setattr(leanctx, "DEFAULT_PI_EXTENSION", tmp_path / "nope.ts")
    captured: dict = {}
    leanctx.launch_pi(LeanCtxConfig(enabled=False), ["-x"],
                      exec_fn=lambda b, a, e: captured.update(argv=a, env=e))
    assert captured["argv"] == ["pi", "-x"]                   # no -e, pi still launches
    assert "LEAN_CTX_HARDEN" not in captured["env"]           # disabled → no lockdown env


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


# ── doctor: check_leanctx enforces the CUI-containment + lockdown invariants ──────────────────
def test_check_leanctx_ok_when_installed_and_locked_down(monkeypatch):
    from secagent.config import Settings
    from secagent.doctor import check_leanctx

    monkeypatch.setattr(leanctx, "binary_installed", lambda: True)
    monkeypatch.setattr(leanctx, "sdk_available", lambda: True)
    c = check_leanctx(Settings())
    assert c.ok and c.severity == "info" and "locked down" in c.detail


def test_check_leanctx_errors_on_routable_endpoint():
    from secagent.config import Settings
    from secagent.doctor import check_leanctx

    s = Settings()
    s.leanctx.endpoint = "http://10.0.0.5:4444"          # would expose CUI prompts
    c = check_leanctx(s)
    assert not c.ok and c.severity == "error" and "loopback" in c.detail


def test_check_leanctx_errors_on_telemetry():
    from secagent.config import Settings
    from secagent.doctor import check_leanctx

    s = Settings()
    s.leanctx.telemetry = True
    c = check_leanctx(s)
    assert not c.ok and c.severity == "error" and "telemetry" in c.detail


def test_check_leanctx_warns_on_persistence(monkeypatch):
    from secagent.config import Settings
    from secagent.doctor import check_leanctx

    monkeypatch.setattr(leanctx, "binary_installed", lambda: True)
    monkeypatch.setattr(leanctx, "sdk_available", lambda: True)
    s = Settings()
    s.leanctx.persist_context = True
    c = check_leanctx(s)
    assert c.ok and c.severity == "warn" and "CUI at rest" in c.detail


def test_check_leanctx_disabled_is_info():
    from secagent.config import Settings
    from secagent.doctor import check_leanctx

    s = Settings()
    s.leanctx.enabled = False
    c = check_leanctx(s)
    assert c.ok and c.severity == "info" and "disabled" in c.detail


# ── CLI: `secagent leanctx` status ───────────────────────────────────────────────────────────
def test_cli_leanctx_status_runs(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from secagent.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))            # isolate ~/.secagent/config.yaml
    r = CliRunner().invoke(app, ["leanctx"])
    assert r.exit_code == 0, r.output
    out = r.output.replace("\n", "")
    assert "LeanCTX" in out and "enabled" in out
    assert "endpoint" in out and "lockdown" in out and "127.0.0.1" in out


def test_cli_leanctx_disabled(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from secagent.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SECAGENT_LEANCTX__ENABLED", "false")
    r = CliRunner().invoke(app, ["leanctx"])
    assert r.exit_code == 0, r.output
    assert "disabled" in r.output.replace("\n", "")


# ── CLI: `secagent pi run` launches pi with LeanCTX, passing args through ──────────────────────
def test_cli_pi_run_launches_with_passthrough_args(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from secagent import leanctx as lc
    from secagent.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    ext = tmp_path / "index.ts"
    ext.write_text("// ext")
    monkeypatch.setenv("SECAGENT_PI_LEANCTX_EXTENSION", str(ext))
    captured: dict = {}
    # Stop the real exec; capture what launch_pi would have run.
    monkeypatch.setattr(lc, "launch_pi",
                        lambda cfg, pi_args, **kw: captured.update(pi_args=pi_args, **kw))

    r = CliRunner().invoke(app, ["pi", "run", "--", "--mode", "rpc", "--name", "x"])
    assert r.exit_code == 0, r.output
    assert captured["pi_args"] == ["--mode", "rpc", "--name", "x"]   # passed straight through
    assert captured["pi_bin"] == "pi"
