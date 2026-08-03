"""Tests for the affordance query functions and the `secagent affordance` CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from secagent.affordances import queries
from secagent.cli import app
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
runner = CliRunner()


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_ensure_indexed_builds_when_missing(tmp_path):
    s = _settings(tmp_path)
    store = queries.ensure_indexed(FIXTURE, s)
    try:
        assert store.load_project_map() is not None
        assert "Python" in queries.structure(store)
        assert queries.io(store)
        comps = json.loads(queries.components(store))
        assert any(c["name"].startswith("services") for c in comps)
        syms = json.loads(queries.find_symbol(store, "get_user"))
        assert any(x["name"] == "get_user" for x in syms)
    finally:
        store.close()


def test_read_slice_traversal_guard(tmp_path):
    s = _settings(tmp_path)
    store = queries.ensure_indexed(FIXTURE, s)
    try:
        assert queries.read_slice(store, "../../etc/passwd", 1, 5).startswith("ERROR")
        ok = queries.read_slice(store, "services/api/db.py", 1, 3)
        assert "1:" in ok
    finally:
        store.close()


def test_cli_affordance_structure(tmp_path, monkeypatch):
    # Point the store at a temp dir via env so we don't write into the fixture.
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    result = runner.invoke(app, ["affordance", "structure", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "Python" in result.output


def test_cli_affordance_search_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SECAGENT_AFFORDANCES__STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SECAGENT_AFFORDANCES__LLM_SUMMARIES", "false")
    # Warm the store first: a cold affordance call auto-indexes and prints a one-time
    # "building index" notice (CliRunner merges stderr into output), which would otherwise
    # precede the JSON. This mirrors real use, where `secagent index` runs before queries.
    runner.invoke(app, ["index", str(FIXTURE), "--no-llm"])
    result = runner.invoke(app, ["affordance", "search", str(FIXTURE), "user database"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any("db.py" in d["path"] for d in data)
