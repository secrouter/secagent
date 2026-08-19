"""Hardening tests for the native-language (C/C++, C#, Rust) call path into the KG.

Two things are verified end to end:

1. The affordance LIGHT backends (libclang for C/C++, tree-sitter for C#/Rust) that
   run with no extra toolchain: a real fixture is indexed and built into a KG, and its
   ``calls`` edges must resolve to the callee's real defining file.
2. The affordance HEAVY backends (Roslyn for C#, rust-analyzer for Rust; C/C++'s heavy
   backend is a later phase per docs/design/heavy-analysis-pipeline.md) resolve calls
   to a qualified symbol and tag dispatch as ``direct``/``virtual``/``interface`` via the
   ``secagent-analysis/v1`` contract (``analysis.ingest_report``). Those backends need a
   container image this environment does not have (`docker image inspect` finds none),
   so this feeds their exact production ingest path with synthetic-but-real-shaped
   reports and asserts the result lands in the KG through
   ``kg.project.project_affordances`` with the right predicate
   (``direct``/``virtual``/``interface`` -> ``calls``/``may_call``/``may_call``) and the
   right qualifier (the callee's defining file) — i.e. the projector boundary the
   affordance layer and the KG actually share.
"""

from __future__ import annotations

from secagent.affordances import analysis, queries
from secagent.affordances.models import CallEdge
from secagent.affordances.rust_ast import rust_available
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings
from secagent.kg import project as kg_project
from secagent.kg.store import KnowledgeGraph


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # deterministic, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _predicate(kg: KnowledgeGraph, caller: str, callee: str) -> str | None:
    row = kg.db.execute(
        "SELECT r.predicate FROM kg_relations r "
        "JOIN kg_entities s ON s.id=r.source_id JOIN kg_entities t ON t.id=r.target_id "
        "WHERE s.name=? AND t.name=?",
        (caller, callee),
    ).fetchone()
    return row["predicate"] if row else None


def _qualifier(kg: KnowledgeGraph, name: str) -> str | None:
    # `kg_entities` has no separate qualifier column — the qualifier is folded into the
    # entity's id (see `entity_id`) and, for a call-edge endpoint, mirrored onto `source`
    # (`project_affordances` sets `source=edge.dst_file`/`sym.file`), which is what a
    # caller can actually read back.
    row = kg.db.execute("SELECT source FROM kg_entities WHERE name=?", (name,)).fetchone()
    return row["source"] if row else None


# -- 1. real fixture through the LIGHT affordance backend, end to end into the KG -------

def test_rust_light_backend_call_edges_resolve_into_kg(tmp_path):
    """A tiny two-file Rust crate, indexed by the real (no-Rust-toolchain-needed)
    tree-sitter backend and built into a KG: `main::process` -> `helper::greet` must
    appear as a `calls` edge qualified by the callee's real defining file."""
    if not rust_available():
        import pytest

        pytest.skip("tree-sitter Rust grammar not installed")

    repo = tmp_path / "crate"
    repo.mkdir()
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = repo / "src"
    src.mkdir()
    (src / "helper.rs").write_text(
        'pub fn greet(name: &str) -> String { format!("hi {}", name) }\n'
    )
    (src / "main.rs").write_text(
        "mod helper;\n"
        "fn process(input: &str) {\n"
        "    let _g = helper::greet(input);\n"
        "}\n"
        "fn main() { process(\"world\"); }\n"
    )

    settings = _settings(tmp_path)
    counts = kg_project.build(repo, settings, store_dir=str(tmp_path / "store"))
    assert counts["relations"] > 0

    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        assert _predicate(kg, "process", "greet") == "calls"
        # Qualified by the callee's real defining file, not process's file.
        assert _qualifier(kg, "greet") == "src/helper.rs"


# -- 2. synthetic RESOLVED affordance reports through the projector boundary ------------

_CSHARP_DIRECT = {
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

# A Rust rust-analyzer report with an INTERFACE (trait) dispatch edge — the case the
# heavy backend exists specifically to distinguish from a plain direct call.
_RUST_INTERFACE = {
    "schema": "secagent-analysis/v1",
    "language": "Rust",
    "backend": "rust-analyzer-scip",
    "functions": [
        {"name": "handle", "qualified_name": "svc::Server::handle",
         "signature": "fn handle(&self, r: Req) -> Resp", "file": "src/server.rs",
         "line": 20, "kind": "method", "owning_type": "svc::Server"},
        {"name": "dispatch", "qualified_name": "svc::Handler::dispatch",
         "signature": "fn dispatch(&self, r: Req) -> Resp", "file": "src/handler.rs",
         "line": 6, "kind": "method", "owning_type": "svc::Handler"},
    ],
    "types": [
        {"qualified_name": "svc::Server", "kind": "struct",
         "interfaces": ["svc::Handler"], "file": "src/server.rs", "line": 10},
        {"qualified_name": "svc::Handler", "kind": "trait", "file": "src/handler.rs", "line": 1},
    ],
    "calls": [
        {"caller_qualified": "svc::Server::handle", "callee_qualified": "svc::Handler::dispatch",
         "callee_file": "src/handler.rs", "line": 22, "edge_kind": "interface"},
    ],
    "build": {"system": "cargo", "restored": True, "offline": True},
}


def test_csharp_heavy_report_direct_call_projects_as_calls(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AffordanceStore(repo, ".secagent")
    try:
        analysis.ingest_report(analysis.parse_report(_CSHARP_DIRECT), store)
        with KnowledgeGraph(repo, store_dir=str(tmp_path / "kgstore")) as kg:
            kg.clear()
            kg_project.project_affordances(kg, store)
            assert _predicate(kg, "Demo.WidgetsController.Get", "Demo.Repo.Count") == "calls"
            assert _qualifier(kg, "Demo.Repo.Count") == "Repo.cs"
    finally:
        store.close()


def test_rust_heavy_report_interface_call_projects_as_may_call(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AffordanceStore(repo, ".secagent")
    try:
        analysis.ingest_report(analysis.parse_report(_RUST_INTERFACE), store)
        with KnowledgeGraph(repo, store_dir=str(tmp_path / "kgstore")) as kg:
            kg.clear()
            kg_project.project_affordances(kg, store)
            assert (
                _predicate(kg, "svc::Server::handle", "svc::Handler::dispatch") == "may_call"
            )
            assert _qualifier(kg, "svc::Handler::dispatch") == "src/handler.rs"
            # The trait relationship itself must also be projected.
            assert _predicate(kg, "svc::Server", "svc::Handler") == "implements"
    finally:
        store.close()


def test_cpp_light_direct_edge_projects_as_calls(tmp_path):
    """C/C++'s only backend today is the light libclang pass (a real heavy clang-build
    backend is a later phase per docs/design/heavy-analysis-pipeline.md), so every C/C++
    call edge it writes is `edge_kind="direct"`. Feeds one such edge — shaped exactly
    like `clang_ast.py`/`call_map.py`'s real output — straight through the store into
    the projector to prove that leg of the boundary too."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        store.set_call_map([
            CallEdge(src_file="main.c", dst_file="util.c",
                     caller="main", callee="do_work", edge_kind="direct"),
        ])
        store.commit()
        with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
            kg.clear()
            kg_project.project_affordances(kg, store)
            assert _predicate(kg, "main", "do_work") == "calls"
            assert _qualifier(kg, "do_work") == "util.c"
    finally:
        store.close()


def test_interface_and_virtual_and_direct_edge_kinds_all_route_correctly(tmp_path):
    """One store carrying all three `edge_kind`s a semantic backend can emit, projected
    together — proving the mapping isn't an artifact of testing one kind in isolation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        store.set_call_map([
            CallEdge(src_file="a.cs", dst_file="b.cs", caller="A.M", callee="B.N",
                     edge_kind="direct"),
            CallEdge(src_file="a.cs", dst_file="c.cs", caller="A.M", callee="C.O",
                     edge_kind="virtual"),
            CallEdge(src_file="a.cs", dst_file="d.cs", caller="A.M", callee="D.P",
                     edge_kind="interface"),
        ])
        store.commit()
        with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
            kg.clear()
            kg_project.project_affordances(kg, store)
            assert _predicate(kg, "A.M", "B.N") == "calls"
            assert _predicate(kg, "A.M", "C.O") == "may_call"
            assert _predicate(kg, "A.M", "D.P") == "may_call"
    finally:
        store.close()
