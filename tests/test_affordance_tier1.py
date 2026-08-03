"""Tier-1 affordances: the reverse call map (`callers`) and the type graph (`types`)."""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.models import CallEdge, TypeRecord
from secagent.affordances.store import AffordanceStore


def _store(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    store.set_call_map([
        CallEdge("apps/fm/fsw/src/fm_app.c", "apps/fm/fsw/src/fm_child.c",
                 caller="FM_AppMain", callee="FM_ChildInit"),
        CallEdge("apps/fm/unit-test/fm_app_tests.c", "apps/fm/fsw/src/fm_app.c",
                 caller="Test_FM_AppMain", callee="FM_AppInit"),
        CallEdge("apps/fm/fsw/src/fm_dispatch.c", "apps/fm/fsw/src/fm_app.c",
                 caller="FM_TaskPipe", callee="FM_AppInit", edge_kind="direct"),
    ])
    store.set_types([
        TypeRecord("Demo.WidgetsController", "class", "Controllers/WidgetsController.cs", 8,
                   bases=["Demo.ControllerBase"], interfaces=["Demo.IWidget"]),
        TypeRecord("Demo.Repo", "class", "Repo.cs", 1),
    ])
    store.commit()
    return store


def test_callers_finds_who_calls_a_function(tmp_path):
    store = _store(tmp_path)
    try:
        out = json.loads(queries.callers(store, "FM_AppInit"))
    finally:
        store.close()
    assert {(o["path"], o["caller"]) for o in out} == {
        ("apps/fm/unit-test/fm_app_tests.c", "Test_FM_AppMain"),
        ("apps/fm/fsw/src/fm_dispatch.c", "FM_TaskPipe"),
    }


def test_callers_none_message(tmp_path):
    store = _store(tmp_path)
    try:
        assert "No callers found" in queries.callers(store, "NoSuchFn")
    finally:
        store.close()


def test_types_lists_inheritance(tmp_path):
    store = _store(tmp_path)
    try:
        out = json.loads(queries.types(store, "Widgets"))
    finally:
        store.close()
    assert len(out) == 1
    assert out[0]["name"] == "Demo.WidgetsController"
    assert out[0]["bases"] == ["Demo.ControllerBase"]
    assert out[0]["interfaces"] == ["Demo.IWidget"]


def test_types_empty_message(tmp_path):
    store = AffordanceStore(tmp_path / "empty", ".secagent")
    try:
        assert "No type graph" in queries.types(store)
    finally:
        store.close()
