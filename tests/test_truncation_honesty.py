"""Every affordance that caps its answer must say so.

Two agentic test cohorts lost time to the same defect class: output that looked complete
and wasn't. Those were found one at a time by devs stumbling into them (a 12-of-20 symbol
list, a call map missing 75% of its edges). This sweeps the rest of the surface rather
than waiting for someone to hit each one.

The rule: a cap is fine — silence about it is not. And a cap that has nothing to hide
must stay quiet, or the notes become noise nobody reads.
"""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.io_map import summarize_io
from secagent.affordances.models import Component, IOEdge, ProjectMap, TypeRecord
from secagent.affordances.store import AffordanceStore

# --- types --------------------------------------------------------------------

def test_types_reports_what_it_capped(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_types([TypeRecord(f"pkg.T{i}", "class", "a.cs", i)
                         for i in range(queries._MAX_ROWS + 20)])
        store.commit()
        out = json.loads(queries.types(store))
        assert "note" in out[-1] and "more types" in out[-1]["note"]
    finally:
        store.close()


def test_types_silent_when_nothing_capped(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_types([TypeRecord("pkg.T", "class", "a.cs", 1)])
        store.commit()
        out = json.loads(queries.types(store))
        assert len(out) == 1 and "note" not in out[0]
    finally:
        store.close()


# --- search -------------------------------------------------------------------

def _repo_with_files(store: AffordanceStore, n: int, word: str) -> None:
    from secagent.affordances.models import FileRecord, FileSummary

    for i in range(n):
        path = f"f{i}.py"
        store.upsert_file(
            FileRecord(path=path, language="Python", size=1, sha256=path, loc=1,
                       n_symbols=0),
            FileSummary(path=path, purpose=f"handles {word} number {i}"),
            [],
        )
    store.commit()


def test_search_reports_files_it_did_not_show(tmp_path):
    """A ranked list that stops at `limit` reads as "these are all the matches"."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo_with_files(store, 25, "widget")
        out = json.loads(queries.search(store, "widget", limit=5))
        assert len(out) == 6                       # 5 results + the note
        assert "+20 more" in out[-1]["note"]
    finally:
        store.close()


def test_search_silent_when_all_matches_shown(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo_with_files(store, 3, "widget")
        out = json.loads(queries.search(store, "widget", limit=15))
        assert len(out) == 3
        assert not any("note" in r for r in out)
    finally:
        store.close()


# --- read_slice ---------------------------------------------------------------

def test_read_slice_says_it_stopped_short(tmp_path):
    """Asking for 1-500 and silently getting 1-200 reads as "the file ends at 200"."""
    (tmp_path / "big.c").write_text("\n".join(f"line{i}" for i in range(1, 601)) + "\n")
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        out = queries.read_slice(store, "big.c", 1, 500)
        assert "line200" in out
        assert "stopped at line 200" in out
        assert "you asked for 500" in out
    finally:
        store.close()


def test_read_slice_silent_when_range_fits(tmp_path):
    (tmp_path / "small.c").write_text("a\nb\nc\n")
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        out = queries.read_slice(store, "small.c", 1, 3)
        assert "stopped at line" not in out
    finally:
        store.close()


# --- IO map -------------------------------------------------------------------

def test_io_reports_per_kind_truncation():
    edges = [IOEdge(f"c{i}", "db", "datastore", "PostgreSQL") for i in range(40)]
    out = summarize_io(edges)
    assert "+10 more datastore edge(s) not shown" in out


def test_io_names_the_kinds_the_line_budget_dropped():
    """The dangerous cap: it exits the whole loop, so a repo with many imports could
    silently lose its datastores entirely — reading as "touches no datastores"."""
    edges = [IOEdge(f"a{i}", f"b{i}", "import") for i in range(200)]
    edges += [IOEdge("svc", "PostgreSQL", "datastore", "PostgreSQL")]
    out = summarize_io(edges, max_lines=20)
    assert "TRUNCATED" in out
    assert "datastore" in out.split("TRUNCATED")[1]     # named as omitted


def test_io_silent_when_everything_fits():
    out = summarize_io([IOEdge("a", "b", "import")])
    assert "not shown" not in out and "TRUNCATED" not in out


# --- project outline ----------------------------------------------------------

def test_outline_reports_dropped_components():
    pm = ProjectMap(
        root=".",
        components=[Component(f"c{i}", f"c{i}/", "package") for i in range(50)],
    )
    out = pm.outline(max_lines=12)
    assert "more component(s) not shown" in out


def test_outline_reports_extra_entrypoints():
    pm = ProjectMap(root=".", entrypoints=[f"e{i}.py" for i in range(15)])
    assert "(+5 more)" in pm.outline()


def test_outline_silent_when_everything_fits():
    pm = ProjectMap(root=".", entrypoints=["main.py"],
                    components=[Component("a", "a/", "package")])
    out = pm.outline()
    assert "more" not in out and "TRUNCATED" not in out


# --- doc pages ----------------------------------------------------------------
# A generated API reference that silently stops reads as a COMPLETE listing, which is
# how a reader concludes a symbol does not exist.

def test_callmap_page_reports_extra_callees():
    from secagent.affordances.models import CallEdge
    from secagent.agents.docs.outline import _MAX_CALLEES_PER_PAIR, _callmap_page

    edges = [CallEdge("a.c", "b.c", "caller", f"callee{i}", "direct")
             for i in range(_MAX_CALLEES_PER_PAIR + 7)]
    body = _callmap_page(edges).body
    assert "+7 more" in body


def test_callmap_page_silent_when_all_callees_shown():
    from secagent.affordances.models import CallEdge
    from secagent.agents.docs.outline import _callmap_page

    body = _callmap_page([CallEdge("a.c", "b.c", "f", "g", "direct")]).body
    assert "more" not in body
