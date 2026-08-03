"""Docs-gen robustness: LLM prose is made safe for reStructuredText, and diagrams are
bounded so a large repo can't produce an unreadable multi-megabyte SVG. Regression
coverage for the docs-gen review findings."""

from __future__ import annotations

from secagent.agents.docs.drawio_gen import (
    _MAX_DIAGRAM_EDGES,
    _MAX_DIAGRAM_NODES,
    Diagram,
    _cap_diagram,
    _Edge,
    _Node,
    to_drawio_xml,
)
from secagent.agents.docs.outline import _safe_prose


def test_safe_prose_escapes_inline_markup():
    # A stray single backtick / asterisk would break a -W Sphinx build.
    out = _safe_prose("the `config value and a * bullet-ish star")
    assert "\\`config" in out and "\\*" in out
    # Every backtick/asterisk is backslash-escaped (none left bare).
    import re
    assert not re.search(r"(?<!\\)[`*]", out)


def test_safe_prose_neutralizes_block_constructs():
    # Markdown rule, RST transition, and a directive-looking line.
    for bad in ["----", "====", ".. danger:: boom", ":: literal"]:
        out = _safe_prose(f"intro line\n{bad}\nmore text")
        line = out.splitlines()[1]
        assert line.startswith("\\"), f"not neutralized: {line!r}"


def test_safe_prose_leaves_normal_text_readable():
    text = "This service handles orders. It talks to Postgres and Redis."
    assert _safe_prose(text) == text


def test_cap_diagram_bounds_large_graph():
    n = 100
    nodes = [_Node(f"c{i}", f"c{i}", "component") for i in range(n)]
    # Hub-and-spoke so c0 is highest-degree and survives the cut.
    edges = [_Edge("c0", f"c{i}") for i in range(1, n)]
    edges += [_Edge(f"c{i}", f"c{i+1}") for i in range(1, n - 1)]
    capped = _cap_diagram(Diagram("big", nodes, edges))
    # Kept nodes are bounded (+1 for the "not shown" note); edges bounded.
    assert len(capped.nodes) <= _MAX_DIAGRAM_NODES + 1
    assert len(capped.edges) <= _MAX_DIAGRAM_EDGES
    # The most-connected hub is retained; a note records what was dropped.
    assert any(x.id == "c0" for x in capped.nodes)
    assert any(x.group == "note" for x in capped.nodes)
    # Every kept edge references a kept node (no dangling endpoints).
    kept = {x.id for x in capped.nodes}
    assert all(e.src in kept and e.dst in kept for e in capped.edges)
    # And it still renders to valid-looking drawio XML.
    assert to_drawio_xml(capped).startswith("<mxfile")


def test_cap_diagram_leaves_small_graph_untouched():
    nodes = [_Node("a", "a", "component"), _Node("b", "b", "component")]
    edges = [_Edge("a", "b")]
    d = Diagram("small", nodes, edges)
    assert _cap_diagram(d) is d


# --- concurrency and priority in the docs describe pass ----------------------

def test_function_descriptions_run_concurrently_without_losing_any():
    """A docs build makes hundreds of these calls serially; that is the 28-minute run.
    Throughput only counts if the output is unchanged."""
    import httpx

    from secagent.affordances.file_summary import describe_functions
    from secagent.affordances.models import Symbol

    from .conftest import make_chat_response, mock_client

    syms = [Symbol(f"fn{i}", "function", "a.c", 1, "", "", "", "") for i in range(8)]
    text = "\n".join(f"int fn{i}(void) {{ return {i}; }}" for i in range(8))

    def run(workers):
        llm = mock_client(lambda r: httpx.Response(
            200, json=make_chat_response(content="Does a thing.")))
        try:
            return describe_functions("a.c", text, syms, llm=llm, workers=workers)
        finally:
            llm.close()

    serial, parallel = run(1), run(4)
    assert set(serial) == set(parallel) == {f"fn{i}" for i in range(8)}


def test_one_failed_description_does_not_lose_the_others():
    import httpx

    from secagent.affordances.file_summary import describe_functions
    from secagent.affordances.models import Symbol

    from .conftest import make_chat_response, mock_client

    syms = [Symbol(f"fn{i}", "function", "a.c", 1, "", "", "", "") for i in range(4)]

    def flaky(request):
        if b"fn0" in request.content:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=make_chat_response(content="Does a thing."))

    llm = mock_client(flaky)
    try:
        out = describe_functions("a.c", "int x;", syms, llm=llm, workers=2)
    finally:
        llm.close()
    assert "fn0" not in out
    assert len(out) == 3


def test_docs_describe_budget_prefers_library_over_examples(tmp_path):
    """On Click the whole 120-function allowance went to `examples/*`.

    This asserted `sorted(["examples/demo.py", "src/click/core.py"], key=path_rank)[0]`
    — it tested `path_rank`, not the describe pass's use of it, and could not fail if
    `_describe_functions` stopped ranking. Now it runs the real pass with the budget
    binding, over a repo whose library file sorts LAST alphabetically, so a pass that
    still follows path order describes the demo and fails here.
    """
    import httpx

    from secagent.affordances.api import index_repo
    from secagent.affordances.store import AffordanceStore
    from secagent.agents.docs.agent import _describe_functions
    from secagent.config import Settings

    from .conftest import make_chat_response, mock_client

    repo = tmp_path / "repo"
    (repo / "examples").mkdir(parents=True)
    (repo / "zlib").mkdir(parents=True)
    (repo / "examples" / "a_demo.py").write_text("def demo_fn():\n    return 1\n")
    (repo / "zlib" / "z_core.py").write_text("def core_fn():\n    return 2\n")

    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    index_repo(repo, s)

    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Does a thing.")))
    store = AffordanceStore(repo, s.affordances.store_dir)
    try:
        described, total = _describe_functions(repo, store, llm, 1)
        assert (described, total) == (1, 2), "the budget must bind or this proves nothing"
        docs_for = {p: [s.doc for s in store.symbols_for_file(p) if s.doc]
                    for p in ("zlib/z_core.py", "examples/a_demo.py")}
        assert docs_for["zlib/z_core.py"], \
            "the one description the budget bought must go to the library file"
        assert not docs_for["examples/a_demo.py"], \
            "the demo must not have consumed the budget"
    finally:
        store.close()
        llm.close()
