"""Store robustness: schema migration, cascading deletes, atomic writes, and
schema-drift-tolerant artifact loads. Regression coverage for the affordance-store
review findings."""

from __future__ import annotations

import json
import sqlite3

from secagent.affordances.models import (
    CallEdge,
    Component,
    FileRecord,
    FileSummary,
    IOEdge,
    TypeRecord,
)
from secagent.affordances.store import AffordanceStore


def _rec(path: str) -> FileRecord:
    return FileRecord(path=path, language="C", size=1, sha256="x", loc=1, n_symbols=0)


def test_migrate_creates_types_table_on_legacy_store(tmp_path):
    """A store predating the `types` table must gain it on open — else load_types()
    crashes with 'no such table: types'."""
    # Simulate a legacy store: files table present (so _SCHEMA won't re-run), no types.
    db_dir = tmp_path / ".secagent"
    db_dir.mkdir()
    con = sqlite3.connect(db_dir / "index.db")
    con.executescript(
        "CREATE TABLE files (path TEXT PRIMARY KEY, language TEXT, size INTEGER,"
        " sha256 TEXT, loc INTEGER, n_symbols INTEGER);"
        "CREATE TABLE summaries (path TEXT PRIMARY KEY, json TEXT);"
        "CREATE TABLE symbols (path TEXT, name TEXT, kind TEXT, lineno INTEGER,"
        " signature TEXT, parent TEXT);"
        "CREATE TABLE calls (src_file TEXT, dst_file TEXT, caller TEXT, callee TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    con.commit()
    con.close()

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        # Must not raise, and set/load must round-trip.
        store.set_types([TypeRecord("pkg.Foo", "class", "a.cs", 1, ["pkg.Base"], [])])
        store.commit()
        got = store.load_types()
        assert [t.qualified_name for t in got] == ["pkg.Foo"]
    finally:
        store.close()


def test_delete_file_clears_inbound_edges_and_types(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.upsert_file(_rec("a.c"), FileSummary(path="a.c"), [])
        store.upsert_file(_rec("b.c"), FileSummary(path="b.c"), [])
        store.set_call_map([
            CallEdge("a.c", "b.c", "fa", "fb", "direct"),  # a -> b (b is dst)
            CallEdge("b.c", "a.c", "fb", "fa", "direct"),  # b -> a (a is dst)
        ])
        store.set_types([TypeRecord("B", "struct", "b.c", 1, [], [])])
        store.commit()

        store.delete_file("b.c")
        store.commit()

        edges = store.load_call_edges()
        # No edge should reference b.c on either end.
        assert all("b.c" not in (e.src_file, e.dst_file) for e in edges), edges
        # b.c's type is gone.
        assert store.load_types() == []
    finally:
        store.close()


def test_artifact_write_is_atomic_no_partial_file(tmp_path):
    """A successful write leaves valid JSON and no leftover temp files."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store._write_artifact("io_map.json", {"edges": [], "components": []})
        art = store.artifacts_dir
        assert json.loads((art / "io_map.json").read_text()) == {
            "edges": [],
            "components": [],
        }
        assert not list(art.glob("*.tmp")), "atomic write left a temp file behind"
    finally:
        store.close()


def test_loads_tolerate_unknown_fields(tmp_path):
    """An artifact/summary carrying a field this build no longer has must not crash
    every read — the unknown key is dropped, known fields survive."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        # Summary with a stray field.
        payload = FileSummary(path="a.c", purpose="hi").to_dict()
        payload["removed_in_a_future_build"] = 123
        store.db.execute(
            "INSERT OR REPLACE INTO summaries VALUES (?,?)", ("a.c", json.dumps(payload))
        )
        store.commit()
        loaded = store.load_summary("a.c")
        assert loaded is not None and loaded.purpose == "hi"

        # Artifacts (io_map + project_map) with stray keys.
        edge = IOEdge("a", "b", "import").to_dict()
        edge["mystery"] = True
        comp = Component("c", "c/", "package").to_dict()
        comp["mystery"] = True
        store._write_artifact("io_map.json", {"edges": [edge], "components": [comp]})
        store._write_artifact(
            "project_map.json",
            {"root": ".", "components": [comp], "future_field": "x"},
        )
        assert store.load_io_edges()[0].dst == "b"
        assert store.load_components()[0].name == "c"
        pm = store.load_project_map()
        assert pm is not None and pm.root == "."
    finally:
        store.close()
