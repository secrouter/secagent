"""Tests for the affordance engine against the fixture sample repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from secagent.affordances.api import index_repo
from secagent.affordances.io_map import component_for, module_name_for, resolve_import
from secagent.affordances.retrieval import ContextBuilder
from secagent.affordances.store import AffordanceStore
from secagent.affordances.symbols import extract_symbols, python_imports
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # no network in tests
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_module_name_for():
    assert module_name_for("services/api/app.py") == "services.api.app"
    assert module_name_for("src/secagent/cli.py") == "secagent.cli"
    assert module_name_for("common/__init__.py") == "common"
    assert module_name_for("README.md") is None


def test_component_for():
    assert component_for("services/api/app.py") == "services/api"
    assert component_for("common/models.py") == "common"
    assert component_for("README.md") == "(root)"
    assert component_for("src/secagent/affordances/models.py") == "secagent/affordances"


def test_resolve_import_relative_and_absolute():
    index = {
        "services.api.db": "services/api/db.py",
        "common.models": "common/models.py",
    }
    assert resolve_import(".db", "services.api.app", index) == "services/api/db.py"
    assert resolve_import("common.models", "services.api.app", index) == "common/models.py"
    assert resolve_import("os", "services.api.app", index) is None


def test_extract_python_symbols():
    text = FIXTURE.joinpath("services/api/db.py").read_text()
    syms = extract_symbols("services/api/db.py", text, "Python")
    names = {s.name for s in syms}
    assert "get_user" in names
    assert "save_user" in names
    assert any(s.signature.startswith("def get_user") for s in syms)


def test_python_imports():
    text = FIXTURE.joinpath("services/api/app.py").read_text()
    imports = python_imports(text)
    assert "common.models" in imports
    assert ".db" in imports


def test_index_repo_builds_complete_store(settings):
    report = index_repo(FIXTURE, settings)
    assert report["files_indexed"] >= 6
    assert "Python" in report["languages"]
    assert report["components"] >= 3
    assert report["io_edges"] > 0
    # worker/main.py is an entrypoint (__main__ guard + main.py name).
    assert any("worker/main.py" in e for e in report["entrypoints"])


def test_index_detects_io_signals(settings):
    index_repo(FIXTURE, settings)
    store = AffordanceStore(FIXTURE, settings.affordances.store_dir)
    try:
        app = store.load_summary("services/api/app.py")
        assert app is not None
        assert "/users/{uid}" in app.endpoints
        assert "API_KEY" in app.env_vars
        assert any("worker" in c for c in app.outbound_calls)

        db = store.load_summary("services/api/db.py")
        assert "SQLite" in db.datastores

        worker = store.load_summary("services/worker/main.py")
        assert "Redis" in worker.datastores
        assert "QUEUE_NAME" in worker.env_vars

        edges = store.load_io_edges()
        # An import edge from the api component to common should exist.
        assert any(e.kind == "import" and "common" in e.dst for e in edges)
        # An HTTP endpoint edge should exist.
        assert any(e.kind == "http_endpoint" for e in edges)
    finally:
        store.close()


def test_index_is_incremental(settings):
    first = index_repo(FIXTURE, settings)
    assert first["updated"] >= 6
    second = index_repo(FIXTURE, settings)
    assert second["updated"] == 0
    assert second["skipped"] >= 6


def test_context_builder_respects_budget(settings):
    index_repo(FIXTURE, settings)
    store = AffordanceStore(FIXTURE, settings.affordances.store_dir)
    try:
        builder = ContextBuilder(store, budget_tokens=120)
        ctx = builder.overview()
        assert builder.counter.count(ctx) <= 140  # budget + small truncation slack
        assert "Project structure" in ctx or "Project:" in ctx

        ranked = builder.rank_summaries("user database sqlite")
        assert ranked
        assert ranked[0].path in {"services/api/db.py", "services/api/app.py"}
    finally:
        store.close()
