"""Phase 0 of the heavy-analysis pipeline: the secagent-analysis/v1 contract + ingest."""

from __future__ import annotations

import pytest

from secagent.affordances import analysis
from secagent.affordances.store import AffordanceStore

_SAMPLE = {
    "schema": "secagent-analysis/v1",
    "language": "C#",
    "backend": "roslyn-msbuild",
    "functions": [
        {"name": "Get", "qualified_name": "Demo.WidgetsController.Get",
         "signature": "IActionResult Get()", "file": "Controllers/WidgetsController.cs",
         "line": 12, "kind": "method", "owning_type": "Demo.WidgetsController"},
        {"name": "Count", "qualified_name": "Demo.Repo.Count", "signature": "int Count()",
         "file": "Repo.cs", "line": 3, "kind": "method", "owning_type": "Demo.Repo"},
    ],
    "types": [
        {"qualified_name": "Demo.WidgetsController", "kind": "class",
         "bases": ["Microsoft.AspNetCore.Mvc.ControllerBase"], "interfaces": [],
         "file": "Controllers/WidgetsController.cs", "line": 8},
        {"qualified_name": "Demo.Repo", "kind": "class", "file": "Repo.cs", "line": 1},
    ],
    "calls": [
        {"caller_qualified": "Demo.WidgetsController.Get", "callee_qualified": "Demo.Repo.Count",
         "callee_file": "Repo.cs", "line": 14, "edge_kind": "direct"},
    ],
    "build": {"system": "msbuild", "restored": True, "offline": True},
}


def test_parse_report_validates():
    r = analysis.parse_report(_SAMPLE)
    assert r.language == "C#" and r.backend == "roslyn-msbuild"
    assert len(r.functions) == 2 and len(r.types) == 2 and len(r.calls) == 1

    with pytest.raises(ValueError):                       # wrong schema
        analysis.parse_report({"schema": "other", "language": "C#", "backend": "x"})
    with pytest.raises(ValueError):                       # missing language
        analysis.parse_report({"schema": "secagent-analysis/v1", "backend": "x"})


def test_parse_ignores_unknown_keys():
    r = analysis.parse_report({**_SAMPLE, "functions": [
        {"name": "F", "qualified_name": "N.F", "signature": "", "file": "a.cs", "line": 1,
         "kind": "method", "owning_type": "N", "future_field": "ignored"}]})
    assert r.functions[0].qualified_name == "N.F"


def test_ingest_report_enriches_store(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        result = analysis.ingest_report(analysis.parse_report(_SAMPLE), store)

        syms = {s.name: s for s in store.symbols_for_file("Controllers/WidgetsController.cs")}
        assert syms["Get"].kind == "method"
        assert syms["Get"].qualified_name == "Demo.WidgetsController.Get"
        assert syms["WidgetsController"].kind == "class"          # type stored as a class symbol

        types = {t.qualified_name: t for t in store.load_types()}
        assert types["Demo.WidgetsController"].bases == ["Microsoft.AspNetCore.Mvc.ControllerBase"]
        assert types["Demo.Repo"].kind == "class"

        edges = store.load_call_edges()
        assert len(edges) == 1
        e = edges[0]
        assert (e.src_file, e.dst_file) == ("Controllers/WidgetsController.cs", "Repo.cs")
        assert e.callee == "Demo.Repo.Count" and e.edge_kind == "direct"

        assert result["functions"] == 2 and result["types"] == 2
        assert result["call_edges"] == 1
    finally:
        store.close()


# `build.restored` must reach `analyze deep`'s printed JSON, not just heavy.py's log line
# (`heavy.py` logs a warning on `restored: false` but the CLI only ever printed
# `ingest_report`'s return dict, which didn't carry it -- so a degraded run was
# byte-identical, in the machine-readable output, to a healthy one).

def test_ingest_report_surfaces_restored_false(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        data = {**_SAMPLE, "build": {"system": "msbuild", "restored": False, "offline": True}}
        result = analysis.ingest_report(analysis.parse_report(data), store)
        assert result["restored"] is False
    finally:
        store.close()


def test_ingest_report_surfaces_restored_true_without_a_warning_shape(tmp_path):
    """Silence test paired with the disclosure above: a fully-restored run reports
    `restored: True` plainly -- present, and not some warning-shaped falsy stand-in."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        result = analysis.ingest_report(analysis.parse_report(_SAMPLE), store)  # restored: True
        assert result["restored"] is True
    finally:
        store.close()


def test_ingest_report_omits_restored_when_the_backend_does_not_report_it(tmp_path):
    """Absence, not a guessed default: a backend that never populates `build.restored`
    (e.g. the Rust backend may not) must not have `True` fabricated on its behalf --
    same house pattern as `callers`' `line` key (`affordances/queries.py`)."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        data = {**_SAMPLE, "build": {"system": "msbuild", "offline": True}}
        result = analysis.ingest_report(analysis.parse_report(data), store)
        assert "restored" not in result
    finally:
        store.close()


# A Rust rust-analyzer report uses the SAME contract, with `::` qualified names and a
# trait impl expressed as an `interfaces` entry — the ingest is backend-agnostic.
_RUST_SAMPLE = {
    "schema": "secagent-analysis/v1",
    "language": "Rust",
    "backend": "rust-analyzer-scip",
    "functions": [
        {"name": "handle", "qualified_name": "svc::Server::handle",
         "signature": "fn handle(&self, r: Req) -> Resp", "file": "src/server.rs",
         "line": 20, "kind": "method", "owning_type": "svc::Server"},
        {"name": "log", "qualified_name": "svc::log::write", "signature": "fn write(s: &str)",
         "file": "src/log.rs", "line": 4, "kind": "function", "owning_type": ""},
    ],
    "types": [
        {"qualified_name": "svc::Server", "kind": "struct",
         "interfaces": ["svc::Handler"], "file": "src/server.rs", "line": 10},
        {"qualified_name": "svc::Handler", "kind": "trait", "file": "src/server.rs", "line": 5},
    ],
    "calls": [
        {"caller_qualified": "svc::Server::handle", "callee_qualified": "svc::log::write",
         "callee_file": "src/log.rs", "line": 22, "edge_kind": "direct"},
    ],
    "build": {"system": "cargo", "restored": True, "offline": True},
}


def test_ingest_rust_report_is_backend_agnostic(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        result = analysis.ingest_report(analysis.parse_report(_RUST_SAMPLE), store)

        syms = {s.name: s for s in store.symbols_for_file("src/server.rs")}
        assert syms["handle"].kind == "method"
        assert syms["handle"].qualified_name == "svc::Server::handle"
        assert syms["Server"].kind == "class"  # a Rust type stored as a class symbol

        types = {t.qualified_name: t for t in store.load_types()}
        assert types["svc::Server"].kind == "struct"
        assert types["svc::Server"].interfaces == ["svc::Handler"]  # trait impl
        assert types["svc::Handler"].kind == "trait"

        edges = store.load_call_edges()
        assert (edges[0].src_file, edges[0].dst_file) == ("src/server.rs", "src/log.rs")
        assert edges[0].callee == "svc::log::write"

        assert result["functions"] == 2 and result["types"] == 2
        assert result["call_edges"] == 1
    finally:
        store.close()
