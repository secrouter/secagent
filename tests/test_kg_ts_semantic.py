"""Tests for the semantic (type-resolved) TS/JS call extractor (secagent.kg.tscalls_semantic).

Skipped whole-file when the Node helper isn't usable (no ``node`` on PATH, or its
vendored ``typescript`` package under ``tools/kg-tscalls`` isn't installed), so the suite
passes with or without that optional toolchain — the same discipline as the jedi/Python
heavy-extractor tests.
"""

from __future__ import annotations

import pytest

from secagent.config import Settings
from secagent.kg import project as kg_project
from secagent.kg.store import KnowledgeGraph
from secagent.kg.tscalls_semantic import ts_semantic_available

pytestmark = pytest.mark.skipif(
    not ts_semantic_available(),
    reason="node + the vendored typescript package (tools/kg-tscalls) are not available",
)


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # deterministic, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _write_repo(repo) -> None:
    repo.mkdir()
    (repo / "a.ts").write_text(
        "export class App {\n"
        "  render(x: string): string {\n"
        "    return x;\n"
        "  }\n"
        "}\n"
    )
    (repo / "b.ts").write_text(
        "export class View {\n"
        "  render(x: string): string {\n"
        "    return x;\n"
        "  }\n"
        "}\n"
    )
    (repo / "use.ts").write_text(
        "import { App } from './a';\n\n"
        "function draw(a: App) {\n"
        "  return a.render('x');\n"
        "}\n"
    )


# -- end-to-end: deep=True actually uses tsc and resolves obj.method() -----------------

def test_deep_build_resolves_obj_method_the_light_path_drops(tmp_path):
    repo = tmp_path / "proj"
    _write_repo(repo)
    settings = _settings(tmp_path)

    # Light (default): `render` is defined in two files (App and View) and the receiver's
    # type is unknown to a name-based resolver, so the call is dropped, not guessed.
    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-light"), deep=False)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-light")) as kg:
        n_light = kg.db.execute(
            "SELECT COUNT(*) FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
            "JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='draw' AND t.name='render' AND r.predicate='calls'"
        ).fetchone()[0]
    assert n_light == 0, "the name-based extractor cannot resolve obj.method() dispatch"

    # Heavy (deep=True): tsc infers `a`'s type (App, from the parameter annotation) and
    # resolves the call to App.render specifically, in a.ts — not View.render in b.ts.
    kg_project.build(repo, settings, store_dir=str(tmp_path / "store-deep"), deep=True)
    with KnowledgeGraph(repo, store_dir=str(tmp_path / "store-deep")) as kg:
        row = kg.db.execute(
            "SELECT t.source FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
            "JOIN kg_entities t ON t.id=r.target_id "
            "WHERE s.name='draw' AND t.name='render' AND r.predicate='calls'"
        ).fetchone()
    assert row is not None, "tsc must resolve a.render() to App.render"
    assert row["source"] == "a.ts"


def test_extract_ts_semantic_returns_edge_count_directly(tmp_path):
    from secagent.affordances import queries
    from secagent.kg.hashing import entity_id
    from secagent.kg.tscalls_semantic import extract_ts_semantic

    repo = tmp_path / "proj"
    _write_repo(repo)
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=settings.affordances.store_dir) as kg:
            kg.clear()
            n = extract_ts_semantic(kg, store)
            assert n >= 1
            # Called directly (no prior projection pass to seed `source`), so assert via
            # the target entity's id — which bakes in its qualifying (defining) file —
            # rather than the `source` column: it resolved to App.render (a.ts), not
            # View.render (b.ts), even though both are named `render`.
            row = kg.db.execute(
                "SELECT r.target_id FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
                "WHERE s.name='draw' AND r.predicate='calls'"
            ).fetchone()
            assert row is not None
            assert row["target_id"] == entity_id("SYMBOL", "render", "a.ts")
    finally:
        store.close()


# -- edge_kind: override-based (virtual) dispatch -> may_call --------------------------


def test_call_to_overridden_method_is_may_call_not_calls(tmp_path):
    """A call statically resolved to a base-class method that a subclass overrides is
    potentially-polymorphic at runtime, so it must land on ``may_call`` — the same
    convention gocalls_semantic.py uses for Go's interface/virtual dispatch — not the
    unconditional ``calls``."""
    from secagent.affordances import queries
    from secagent.kg.tscalls_semantic import extract_ts_semantic

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "shapes.ts").write_text(
        "export class Shape {\n"
        "  area(): number {\n"
        "    return 0;\n"
        "  }\n"
        "}\n"
        "export class Circle extends Shape {\n"
        "  area(): number {\n"
        "    return 3;\n"
        "  }\n"
        "}\n"
    )
    (repo / "use.ts").write_text(
        "import { Shape } from './shapes';\n\n"
        "function measure(s: Shape) {\n"
        "  return s.area();\n"
        "}\n"
    )
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=settings.affordances.store_dir) as kg:
            kg.clear()
            n = extract_ts_semantic(kg, store)
            assert n >= 1
            row = kg.db.execute(
                "SELECT r.predicate FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
                "WHERE s.name='measure' AND r.predicate IN ('calls', 'may_call')"
            ).fetchone()
            assert row is not None
            assert row["predicate"] == "may_call", (
                "Shape.area is overridden by Circle.area, so the statically-resolved "
                "call is potentially-polymorphic and must not be asserted as 'calls'"
            )
    finally:
        store.close()


def test_call_to_non_overridden_method_stays_calls(tmp_path):
    """The non-override case (no subclass in the repo shadows the resolved method) must
    keep the unconditional ``calls`` predicate — the fix must not over-tag every method
    call on a class as potentially virtual."""
    from secagent.affordances import queries
    from secagent.kg.tscalls_semantic import extract_ts_semantic

    repo = tmp_path / "proj"
    _write_repo(repo)  # App/View have no inheritance relationship
    settings = _settings(tmp_path)
    store = queries.ensure_indexed(repo, settings)
    try:
        with KnowledgeGraph(repo, store_dir=settings.affordances.store_dir) as kg:
            kg.clear()
            n = extract_ts_semantic(kg, store)
            assert n >= 1
            row = kg.db.execute(
                "SELECT r.predicate FROM kg_relations r JOIN kg_entities s ON s.id=r.source_id "
                "WHERE s.name='draw' AND r.predicate IN ('calls', 'may_call')"
            ).fetchone()
            assert row is not None
            assert row["predicate"] == "calls"
    finally:
        store.close()
