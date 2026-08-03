"""Tier-2 affordance polish: component purposes, scoped summaries, qualified names."""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.models import Component, FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore


def _store(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    store.upsert_file(
        FileRecord(path="apps/fm/fsw/src/fm_app.c", language="C", size=10, sha256="x", loc=5),
        FileSummary(path="apps/fm/fsw/src/fm_app.c", purpose="FM main application loop",
                    source="llm"),
        [Symbol("FM_AppMain", "function", "apps/fm/fsw/src/fm_app.c", 1,
                signature="void FM_AppMain(void)")],
    )
    store.upsert_file(
        FileRecord(path="Repo.cs", language="C#", size=10, sha256="y", loc=5),
        FileSummary(path="Repo.cs", purpose="Widget repository"),
        [Symbol("Count", "method", "Repo.cs", 3, signature="int Count()",
                qualified_name="Demo.Repo.Count")],
    )
    store.set_io_map(
        [Component(name="apps/fm", path="apps/fm", kind="package",
                   files=["apps/fm/fsw/src/fm_app.c"], language="C")],
        [],
    )
    store.commit()
    return store


def test_components_include_representative_purpose(tmp_path):
    store = _store(tmp_path)
    try:
        out = json.loads(queries.components(store))
    finally:
        store.close()
    fm = next(c for c in out if c["name"] == "apps/fm")
    assert fm["purpose"] == "FM main application loop"
    assert fm["files"] == 1 and fm["language"] == "C"


def test_components_skip_placeholder_purpose(tmp_path):
    # A short LICENSE ("Other file (N bytes)." placeholder) must not win over the real app
    # purpose — the bug seen on cFS where every app showed "Other file (11357 bytes).".
    store = AffordanceStore(tmp_path, ".secagent")
    store.upsert_file(
        FileRecord(path="apps/cf/LICENSE", language="Other", size=11357, sha256="a", loc=0),
        FileSummary(path="apps/cf/LICENSE", purpose="Other file (11357 bytes)."),
        [],
    )
    store.upsert_file(
        FileRecord(path="apps/cf/fsw/src/cf_app.c", language="C", size=10, sha256="b", loc=5),
        FileSummary(path="apps/cf/fsw/src/cf_app.c", purpose="CFDP file transfer app",
                    source="llm"),
        [],
    )
    store.set_io_map(
        [Component(name="apps/cf", path="apps/cf", kind="package",
                   files=["apps/cf/LICENSE", "apps/cf/fsw/src/cf_app.c"], language="C")],
        [],
    )
    store.commit()
    try:
        out = json.loads(queries.components(store))
    finally:
        store.close()
    cf = next(c for c in out if c["name"] == "apps/cf")
    assert cf["purpose"] == "CFDP file transfer app"


def test_functions_include_qualified_name_only_when_present(tmp_path):
    store = _store(tmp_path)
    try:
        cs = json.loads(queries.functions(store, "Repo.cs"))
        c = json.loads(queries.functions(store, "apps/fm/fsw/src/fm_app.c"))
    finally:
        store.close()
    assert cs[0]["qualified_name"] == "Demo.Repo.Count"
    assert "qualified_name" not in c[0]  # empty qualified name is omitted


def test_summaries_scoped_to_component(tmp_path):
    store = _store(tmp_path)
    try:
        scoped = json.loads(queries.summaries(store, component="apps/fm"))
        full = json.loads(queries.summaries(store))
    finally:
        store.close()
    assert set(scoped["files"]) == {"apps/fm/fsw/src/fm_app.c"}
    assert "Repo.cs" in full["files"]
