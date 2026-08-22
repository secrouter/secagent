"""Tests for the light-vs-heavy call-extractor registry (secagent.kg.extractors) and
the edge_kind -> predicate mapping it feeds into the graph."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from secagent.affordances.models import CallEdge
from secagent.config import Settings
from secagent.kg import extractors
from secagent.kg import project as kg_project
from secagent.kg.store import KnowledgeGraph


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # deterministic, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


# -- selection: a minimal fake store, just enough for run_extractors' own inputs --------

@dataclass
class _FakeRec:
    path: str
    language: str


@dataclass
class _FakeStore:
    records: list[_FakeRec]
    call_edges: list[CallEdge] = field(default_factory=list)

    def file_records(self) -> list[_FakeRec]:
        return self.records

    def load_call_edges(self) -> list[CallEdge]:
        return self.call_edges


def _spy(tag: str, calls: list[str], n: int = 1):
    def fn(kg, store) -> int:  # noqa: ARG001 — matches the extractor contract
        calls.append(tag)
        return n
    return fn


def test_selects_light_by_default(monkeypatch):
    calls: list[str] = []
    monkeypatch.setitem(extractors.REGISTRY, "python", extractors.LanguageExtractors(
        light=_spy("light", calls, 1),
        heavy_loader=lambda: (_spy("heavy", calls, 99), lambda: True),
    ))
    store = _FakeStore([_FakeRec("a.py", "Python")])
    total = extractors.run_extractors(None, store, deep=False)
    assert calls == ["light"]
    assert total == 1


def test_uses_heavy_when_deep_and_available(monkeypatch):
    calls: list[str] = []
    monkeypatch.setitem(extractors.REGISTRY, "python", extractors.LanguageExtractors(
        light=_spy("light", calls, 1),
        heavy_loader=lambda: (_spy("heavy", calls, 99), lambda: True),
    ))
    store = _FakeStore([_FakeRec("a.py", "Python")])
    total = extractors.run_extractors(None, store, deep=True)
    assert calls == ["heavy"]
    assert total == 99


def test_unavailable_heavy_falls_back_to_light(monkeypatch):
    # The heavy loader resolves (the module exists) but reports itself unusable.
    calls: list[str] = []
    monkeypatch.setitem(extractors.REGISTRY, "python", extractors.LanguageExtractors(
        light=_spy("light", calls, 1),
        heavy_loader=lambda: (_spy("heavy", calls, 99), lambda: False),
    ))
    store = _FakeStore([_FakeRec("a.py", "Python")])
    total = extractors.run_extractors(None, store, deep=True)
    assert calls == ["light"]
    assert total == 1


def test_missing_heavy_module_falls_back_to_light(monkeypatch):
    # The heavy loader itself returns None — the module hasn't been built yet (the
    # ImportError-swallowing case a not-yet-added Go/TS heavy module hits).
    calls: list[str] = []
    monkeypatch.setitem(extractors.REGISTRY, "python", extractors.LanguageExtractors(
        light=_spy("light", calls, 1),
        heavy_loader=lambda: None,
    ))
    store = _FakeStore([_FakeRec("a.py", "Python")])
    total = extractors.run_extractors(None, store, deep=True)
    assert calls == ["light"]
    assert total == 1


def test_language_already_resolved_by_affordances_is_skipped(monkeypatch):
    # A heavy AFFORDANCE backend (clang/csharp/rust/...) already wrote resolved `calls`
    # edges for this language's files — the light KG extractor must not also run.
    calls: list[str] = []
    monkeypatch.setitem(extractors.REGISTRY, "python", extractors.LanguageExtractors(
        light=_spy("light", calls, 1),
        heavy_loader=lambda: None,
    ))
    store = _FakeStore(
        [_FakeRec("a.py", "Python")],
        call_edges=[CallEdge(src_file="a.py", dst_file="a.py", caller="f", callee="g")],
    )
    total = extractors.run_extractors(None, store, deep=False)
    assert calls == []
    assert total == 0


def test_shared_light_extractor_runs_once_across_languages(monkeypatch):
    # Go/TS/JS share one light callable; a repo with more than one of those languages
    # must run it exactly once, not once per language.
    calls: list[str] = []
    shared = _spy("shared", calls, 5)
    monkeypatch.setitem(
        extractors.REGISTRY, "go",
        extractors.LanguageExtractors(light=shared, heavy_loader=lambda: None),
    )
    monkeypatch.setitem(
        extractors.REGISTRY, "typescript",
        extractors.LanguageExtractors(light=shared, heavy_loader=lambda: None),
    )
    store = _FakeStore([_FakeRec("a.go", "Go"), _FakeRec("b.ts", "TypeScript")])
    total = extractors.run_extractors(None, store, deep=False)
    assert calls == ["shared"]
    assert total == 5


# -- cross-language: a heavy-covered language must not get light-path duplicates -------

def test_heavy_covered_language_gets_no_light_duplicate_in_mixed_repo(tmp_path, monkeypatch):
    """Go+TS repo, deep=True, TS heavy forced unavailable. Before the fix, the light
    treesitter pass (selected for TS) walked *all* Go/TS/JS files via the shared
    extension map with no language filter, so Go's interface-dispatch call
    (viaInterface -> Greet) — already correctly resolved by the Go heavy extractor as
    the honest, possibly-polymorphic ``may_call`` — also got a spurious unconditional
    ``calls`` edge from the name-based light pass. It must not."""
    from secagent.kg.gocalls_semantic import go_semantic_available

    if not go_semantic_available():
        pytest.skip("go toolchain + tools/kg-gocallgraph helper not available")

    monkeypatch.setattr(extractors, "_load_ts_heavy", lambda: None)

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "go.mod").write_text("module fixture\n\ngo 1.21\n")
    (repo / "main.go").write_text(
        "package main\n\n"
        "type Greeter interface {\n"
        "\tGreet() string\n"
        "}\n\n"
        "type English struct{}\n\n"
        "func (English) Greet() string { return \"hi\" }\n\n"
        "func viaInterface(g Greeter) string { return g.Greet() }\n\n"
        "func main() {\n"
        "\tprintln(viaInterface(English{}))\n"
        "}\n"
    )
    (repo / "util.ts").write_text("export function noop(): void {}\n")
    settings = _settings(tmp_path)

    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-deep"), deep=True)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-deep")) as kg:
        rows = kg.db.execute(
            "SELECT r.predicate FROM kg_relations r "
            "JOIN kg_entities s ON s.id=r.source_id JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='viaInterface' AND t.name='Greet'"
        ).fetchall()
    predicates = {row["predicate"] for row in rows}
    assert "calls" not in predicates, (
        "the light treesitter pass must not add an unconditional 'calls' duplicate "
        "for a call the Go heavy extractor already resolved as polymorphic"
    )
    assert predicates == {"may_call"}, "the semantic may_call edge must still be present"


# -- end-to-end: deep=True actually uses jedi and resolves obj.method() -----------------

def test_deep_build_resolves_obj_method_the_light_path_drops(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "a.py").write_text(
        "class App:\n    def render(self, x):\n        return x\n"
    )
    (repo / "b.py").write_text(
        "class View:\n    def render(self, x):\n        return x\n"
    )
    (repo / "use.py").write_text(
        "from a import App\n\n\ndef draw():\n    thing = App()\n    return thing.render('x')\n"
    )
    settings = _settings(tmp_path)

    # Light (default): `render` is defined in two files and the receiver's type is
    # unknown to a name-based resolver, so the call is dropped, not guessed.
    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-light"), deep=False)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-light")) as kg:
        n_light = kg.db.execute(
            "SELECT COUNT(*) FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
            "JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='draw' AND t.name='render' AND r.predicate='calls'"
        ).fetchone()[0]
    assert n_light == 0, "the name-based extractor cannot resolve obj.method() dispatch"

    # Heavy (deep=True): jedi infers `thing`'s type (App, from the local assignment)
    # and resolves the call to App.render specifically, in a.py.
    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-deep"), deep=True)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-deep")) as kg:
        row = kg.db.execute(
            "SELECT t.source FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
            "JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='draw' AND t.name='render' AND r.predicate='calls'"
        ).fetchone()
    assert row is not None, "jedi must resolve thing.render() to App.render"
    assert row["source"] == "a.py"


# -- edge_kind -> predicate mapping ------------------------------------------------------

def test_virtual_affordance_edge_projects_as_may_call(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "a.py").write_text("def base_call():\n    pass\n")
    (repo / "b.py").write_text("def impl_call():\n    pass\n")
    settings = _settings(tmp_path)

    from secagent.affordances import queries

    store = queries.ensure_indexed(repo, settings)
    try:
        store.set_call_map([
            CallEdge(
                src_file="a.py", dst_file="b.py",
                caller="base_call", callee="impl_call", edge_kind="virtual",
            ),
        ])
        store.commit()
        with KnowledgeGraph(repo, store_dir=str(tmp_path / "store")) as kg:
            kg.clear()
            kg_project.project_affordances(kg, store)
            row = kg.db.execute(
                "SELECT r.predicate FROM kg_relations r "
                "JOIN kg_entities s ON s.id=r.source_id JOIN kg_entities t ON t.id=r.target_id "
                "WHERE s.name='base_call' AND t.name='impl_call'"
            ).fetchone()
            assert row is not None
            assert row["predicate"] == "may_call"
    finally:
        store.close()
