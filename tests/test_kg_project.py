"""Tests for the deterministic code projector: affordances -> knowledge graph,
end to end over the sample repo, including a real recall."""

from __future__ import annotations

from pathlib import Path

from secagent.config import Settings
from secagent.kg import KnowledgeGraph
from secagent.kg import project as kg_project
from secagent.kg.recall import recall

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # deterministic, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_build_projects_affordances_into_the_graph(tmp_path):
    settings = _settings(tmp_path)
    counts = kg_project.build(FIXTURE, settings, store_dir=str(tmp_path / "store"))
    assert counts["entities"] > 0
    assert counts["relations"] > 0

    with KnowledgeGraph(FIXTURE, store_dir=str(tmp_path / "store")) as kg:
        # Files and symbols both projected, with defined_in edges connecting them.
        types = {r["type"] for r in kg.db.execute("SELECT DISTINCT type FROM kg_entities")}
        assert "FILE" in types and "SYMBOL" in types
        rels = kg.db.execute("SELECT DISTINCT predicate FROM kg_relations")
        preds = {r["predicate"] for r in rels}
        assert "defined_in" in preds


def test_recall_finds_a_real_symbol_and_its_file(tmp_path):
    settings = _settings(tmp_path)
    kg_project.build(FIXTURE, settings, store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(FIXTURE, store_dir=str(tmp_path / "store")) as kg:
        r = recall(kg, "where is get_user defined and what does it call")
        assert not r.is_empty()
        text = r.as_text()
        assert "get_user" in text
        # get_user is a symbol; it must have a defined_in edge to its file.
        assert any(
            f.subject == "get_user" and f.predicate == "defined_in" for f in r.facts
        ), text


def test_build_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    first = kg_project.build(FIXTURE, settings, store_dir=str(tmp_path / "store"))
    second = kg_project.build(FIXTURE, settings, store_dir=str(tmp_path / "store"))
    assert first == second, "a re-project of unchanged code must yield identical counts"


def test_python_call_edges_are_extracted(tmp_path):
    # The affordance light path leaves Python `calls` empty; the ast extractor fills them.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "mod.py").write_text(
        "def helper():\n    return 1\n\n"
        "def main():\n    helper()\n    return helper()\n"
    )
    settings = _settings(tmp_path)
    counts = kg_project.build(repo, settings, store_dir=str(tmp_path / "store"))
    assert counts["relations"] > 0
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        n_calls = kg.db.execute(
            "SELECT COUNT(*) FROM kg_relations WHERE predicate='calls'"
        ).fetchone()[0]
        assert n_calls >= 1, "main -> helper call edge must be extracted"
        r = recall(kg, "what calls helper")
        triples = {(f.subject, f.predicate, f.obj) for f in r.facts}
        assert ("main", "calls", "helper") in triples


def test_full_signature_is_stored(tmp_path):
    # Keyword-only args, annotations, and the return type must survive into the KG so
    # "what breaks if I change this signature" has the real params to reason about.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text("def f(a, *, b: int = 1) -> int:\n    return a\n")
    kg_project.build(repo, _settings(tmp_path), store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
        desc = kg.db.execute("SELECT description FROM kg_entities WHERE name='f'").fetchone()[0]
        assert "b: int" in desc  # keyword-only arg preserved
        assert "-> int" in desc  # return annotation preserved


def test_basename_alias_seeds_a_file(tmp_path):
    settings = _settings(tmp_path)
    kg_project.build(FIXTURE, settings, store_dir=str(tmp_path / "store"))
    with KnowledgeGraph(FIXTURE, store_dir=str(tmp_path / "store")) as kg:
        # "db.py" is a basename alias for services/api/db.py.
        seeds = kg.seed(["db.py"])
        names = {kg.get_entity(s).name for s in seeds}
        assert any(n.endswith("db.py") for n in names)
