"""Tests for the semantic (type-resolved) Go call extractor (secagent.kg.gocalls_semantic).

Skipped whole-file when the helper isn't usable (no ``go`` on PATH, or the helper source
under ``tools/kg-gocallgraph`` isn't present — e.g. a wheel install that only ships
``src/secagent``), so the suite passes with or without that optional toolchain — the
same discipline as the jedi/Python and tsc/TS heavy-extractor tests.
"""

from __future__ import annotations

import pytest

from secagent.config import Settings
from secagent.kg import project as kg_project
from secagent.kg.gocalls_semantic import go_semantic_available
from secagent.kg.store import KnowledgeGraph

pytestmark = pytest.mark.skipif(
    not go_semantic_available(),
    reason="go toolchain + the vendored helper source (tools/kg-gocallgraph) are not available",
)


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # deterministic, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _write_repo(repo) -> None:
    """A tiny Go module: one func calls another directly, and a method is called
    through an interface value — the two dispatch shapes the light (tree-sitter, name
    only) extractor cannot tell apart from an ordinary call."""
    repo.mkdir()
    (repo / "go.mod").write_text("module fixture\n\ngo 1.21\n")
    (repo / "main.go").write_text(
        "package main\n\n"
        "type Greeter interface {\n"
        "\tGreet() string\n"
        "}\n\n"
        "type English struct{}\n\n"
        "func (English) Greet() string { return helper() }\n\n"
        "func helper() string { return \"hi\" }\n\n"
        "func direct() string { return helper() }\n\n"
        "func viaInterface(g Greeter) string { return g.Greet() }\n\n"
        "func main() {\n"
        "\tprintln(direct())\n"
        "\tprintln(viaInterface(English{}))\n"
        "}\n"
    )


# -- end-to-end: deep=True actually resolves the call graph -----------------------------

def test_deep_build_resolves_direct_and_interface_calls(tmp_path):
    repo = tmp_path / "proj"
    _write_repo(repo)
    settings = _settings(tmp_path)

    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-deep"), deep=True)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-deep")) as kg:
        direct_row = kg.db.execute(
            "SELECT r.predicate FROM kg_relations r "
            "JOIN kg_entities s ON s.id=r.source_id JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='direct' AND t.name='helper'"
        ).fetchone()
        assert direct_row is not None, "the direct call to helper() must resolve"
        assert direct_row["predicate"] == "calls"

        # The interface call (viaInterface -> g.Greet()) resolves to English.Greet, an
        # honestly-polymorphic edge (could be any Greeter implementer) -> may_call.
        iface_row = kg.db.execute(
            "SELECT r.predicate FROM kg_relations r "
            "JOIN kg_entities s ON s.id=r.source_id JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='viaInterface' AND t.name='Greet'"
        ).fetchone()
        assert iface_row is not None, "the interface call g.Greet() must resolve to English.Greet"
        assert iface_row["predicate"] == "may_call"


def test_extract_go_semantic_returns_edge_count_directly(tmp_path):
    from secagent.affordances import queries
    from secagent.kg.gocalls_semantic import extract_go_semantic

    repo = tmp_path / "proj"
    _write_repo(repo)
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=settings.affordances.store_dir) as kg:
            kg.clear()
            n = extract_go_semantic(kg, store)
            assert n >= 2  # at least direct() -> helper() and viaInterface() -> Greet
    finally:
        store.close()


def test_extract_go_semantic_no_go_mod_is_a_noop(tmp_path):
    from secagent.affordances import queries
    from secagent.kg.gocalls_semantic import extract_go_semantic

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=settings.affordances.store_dir) as kg:
            kg.clear()
            assert extract_go_semantic(kg, store) == 0
    finally:
        store.close()
