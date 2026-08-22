"""Tests for Go/TypeScript call-edge extraction (tree-sitter). Skipped when the
grammar extras aren't installed, so the suite passes with or without them."""

from __future__ import annotations

import pytest

from secagent.config import Settings
from secagent.kg import KnowledgeGraph
from secagent.kg import project as kg_project
from secagent.kg.recall import recall


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _calls(kg: KnowledgeGraph) -> int:
    return kg.db.execute("SELECT COUNT(*) FROM kg_relations WHERE predicate='calls'").fetchone()[0]


def test_go_call_edges_are_extracted(tmp_path):
    pytest.importorskip("tree_sitter_go")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "main.go").write_text(
        "package main\n\n"
        "func helper() int { return 1 }\n\n"
        "func run() int {\n\treturn helper() + helper()\n}\n"
    )
    kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        assert _calls(kg) >= 1
        r = recall(kg, "what calls helper")
        assert any(f.subject == "run" and f.obj == "helper" for f in r.facts)


def test_go_method_call_resolves_by_name(tmp_path):
    pytest.importorskip("tree_sitter_go")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "s.go").write_text(
        "package main\n\n"
        "type S struct{}\n\n"
        "func (s S) OfferLeg() {}\n\n"
        "func handle(s S) { s.OfferLeg() }\n"
    )
    kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        # receiver.Method() resolves to the bare method name; qualifier-strip seeding lets
        # a prompt written as "S.OfferLeg" find it.
        r = recall(kg, "what calls S.OfferLeg")
        assert any(f.subject == "handle" and f.obj == "OfferLeg" for f in r.facts)


def test_ts_call_edges_including_arrow_functions(tmp_path):
    pytest.importorskip("tree_sitter_typescript")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.ts").write_text(
        "function helper(): number { return 1; }\n"
        "export function run(): number { return helper() + helper(); }\n"
        "const arrow = () => helper();\n"
    )
    kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        triples = {
            (f.subject, f.obj)
            for f in recall(kg, "what calls helper").facts
            if f.predicate == "calls"
        }
        assert ("run", "helper") in triples
        assert ("arrow", "helper") in triples  # const f = () => … caller attribution


def test_ts_member_call_resolves_by_property(tmp_path):
    pytest.importorskip("tree_sitter_typescript")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "hub.ts").write_text(
        "function broadcast() {}\n"
        "function announce(hub: any) { hub.broadcast(); }\n"
    )
    kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        r = recall(kg, "what calls broadcast")
        assert any(f.subject == "announce" and f.obj == "broadcast" for f in r.facts)


def test_build_without_grammars_still_succeeds(tmp_path, monkeypatch):
    # When a grammar is unavailable, extraction is a no-op and the build still works.
    import secagent.kg.treesitter_calls as tsc

    monkeypatch.setattr(tsc, "_available", lambda key: False)
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "main.go").write_text("package main\nfunc run() {}\n")
    counts = kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    assert counts["entities"] >= 1  # symbols/files still projected; just no Go call edges
