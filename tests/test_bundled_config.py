"""Bundled-default config paths (scan rules, review persona) must resolve regardless of
the working directory — pi runs secagent from the project dir, not the secagent repo."""

from __future__ import annotations

from secagent.config import resolve_config_path


def test_resolves_bundled_default_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # NOT the secagent repo root
    p = resolve_config_path("config/rules/embedded-cpp.yaml")
    assert p.exists(), p
    assert p.name == "embedded-cpp.yaml"


def test_absolute_path_unchanged(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("rules: []\n")
    assert resolve_config_path(str(f)) == f


def test_missing_relative_returned_as_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = resolve_config_path("config/rules/nope.yaml")
    assert not p.exists()  # returned as-given so the caller's error is sensible


def test_load_rules_default_from_other_cwd(tmp_path, monkeypatch):
    from secagent.agents.scan.rules import load_rules

    monkeypatch.chdir(tmp_path)
    ruleset = load_rules("config/rules/embedded-cpp.yaml")  # the default, from elsewhere
    assert ruleset.rules  # actually loaded the bundled rules
