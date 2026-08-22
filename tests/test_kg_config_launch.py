"""Tests for the KG config surface and the `secagent pi run` auto-attach wiring."""

from __future__ import annotations

from typer.testing import CliRunner

from secagent import leanctx as lc
from secagent.cli import _kg_extension_path, app
from secagent.config import Settings, load_settings

runner = CliRunner()


def test_knowledge_graph_config_defaults():
    kg = Settings().knowledge_graph
    assert kg.inject is False
    assert kg.hops == 3
    assert kg.top_k == 8
    assert kg.extension == ""


def test_yaml_knowledge_graph_section_is_accepted(tmp_path, monkeypatch):
    # The section must be registered on Settings AND _pristine_dump, or load_settings
    # rejects it as an unknown section.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("knowledge_graph:\n  inject: true\n  hops: 2\n  top_k: 4\n")
    s = load_settings(str(cfg))
    assert s.knowledge_graph.inject is True
    assert s.knowledge_graph.hops == 2
    assert s.knowledge_graph.top_k == 4


def test_kg_extension_path_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("SECAGENT_KG_EXTENSION", raising=False)
    ext = tmp_path / "secagent-kg.ts"
    ext.write_text("// stub")
    assert _kg_extension_path(str(ext)) == ext
    assert _kg_extension_path("") is None
    assert _kg_extension_path(str(tmp_path / "missing.ts")) is None
    # The env override wins over a config value.
    monkeypatch.setenv("SECAGENT_KG_EXTENSION", str(ext))
    assert _kg_extension_path("") == ext


def _capture_launch(monkeypatch) -> dict:
    captured: dict = {}

    def fake_launch(cfg, pi_args, *, pi_bin="pi", extra_env=None, exec_fn=None):
        captured["pi_args"] = pi_args
        captured["extra_env"] = extra_env
        return pi_args

    monkeypatch.setattr(lc, "launch_pi", fake_launch)
    monkeypatch.setenv("SECAGENT_LEANCTX__ENABLED", "false")  # isolate from LeanCTX attach
    return captured


def test_pi_run_auto_attaches_kg_extension_when_injecting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ext = tmp_path / "secagent-kg.ts"
    ext.write_text("// stub")
    captured = _capture_launch(monkeypatch)
    monkeypatch.setenv("SECAGENT_KNOWLEDGE_GRAPH__INJECT", "true")
    monkeypatch.setenv("SECAGENT_KNOWLEDGE_GRAPH__HOPS", "2")
    monkeypatch.setenv("SECAGENT_KNOWLEDGE_GRAPH__TOP_K", "5")
    monkeypatch.setenv("SECAGENT_KG_EXTENSION", str(ext))

    result = runner.invoke(app, ["pi", "run"])
    assert result.exit_code == 0, result.output
    assert "-e" in captured["pi_args"]
    assert str(ext) in captured["pi_args"]
    # The extension is prepended so it loads (before any pass-through pi args).
    assert captured["pi_args"][:2] == ["-e", str(ext)]
    assert captured["extra_env"]["SECAGENT_KG_HOPS"] == "2"
    assert captured["extra_env"]["SECAGENT_KG_TOP_K"] == "5"


def test_pi_run_no_kg_attach_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    captured = _capture_launch(monkeypatch)
    monkeypatch.delenv("SECAGENT_KNOWLEDGE_GRAPH__INJECT", raising=False)

    result = runner.invoke(app, ["pi", "run"])
    assert result.exit_code == 0, result.output
    assert "-e" not in captured["pi_args"]  # nothing KG-related attached
    assert not captured["extra_env"]


def test_pi_run_injects_nothing_when_extension_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    captured = _capture_launch(monkeypatch)
    monkeypatch.setenv("SECAGENT_KNOWLEDGE_GRAPH__INJECT", "true")
    monkeypatch.setenv("SECAGENT_KG_EXTENSION", str(tmp_path / "does-not-exist.ts"))

    result = runner.invoke(app, ["pi", "run"])
    assert result.exit_code == 0, result.output
    # inject on but no resolvable extension: pi still launches, just without KG.
    assert "-e" not in captured["pi_args"]
