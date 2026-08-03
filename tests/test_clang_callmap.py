"""clang AST extraction, the inter-file call map, and call-aware store columns."""

from __future__ import annotations

import pytest

from secagent.affordances.call_map import (
    build_definition_table,
    is_test_path,
    resolve_calls,
)
from secagent.affordances.clang_ast import CallSite, FuncDef, clang_available, parse_file
from secagent.affordances.models import CallEdge, FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore


def test_definition_table_prefers_non_test():
    funcs = [
        FuncDef("foo", "void foo()", "unit-test/stubs/x_stub.c", 1),
        FuncDef("foo", "void foo()", "fsw/src/x.c", 1),
    ]
    assert build_definition_table(funcs)["foo"] == "fsw/src/x.c"


def test_resolve_calls_dedups_to_file_edges():
    defs = {"bar": "src/b.c"}
    calls = [CallSite("foo", "bar", "src/a.c", 3), CallSite("foo", "bar", "src/a.c", 9)]
    edges = resolve_calls(calls, defs)
    assert len(edges) == 1
    e = edges[0]
    assert (e.src_file, e.dst_file, e.callee) == ("src/a.c", "src/b.c", "bar")


def test_resolve_skips_self_and_unknown():
    defs = {"local": "src/a.c"}  # callee defined in the same file -> not an edge
    calls = [CallSite("f", "local", "src/a.c", 1), CallSite("f", "external", "src/a.c", 2)]
    assert resolve_calls(calls, defs) == []


def test_is_test_path():
    assert is_test_path("apps/x/unit-test/stubs/y_stub.c")
    assert not is_test_path("apps/x/fsw/src/y.c")


def test_store_persists_doc_and_call_map(tmp_path):
    st = AffordanceStore(tmp_path, ".store")
    st.upsert_file(
        FileRecord("a.c", "C", 10, "sha", 1, 1),
        FileSummary("a.c"),
        [Symbol("foo", "function", "a.c", 1, "void foo()")],
    )
    st.set_symbol_doc("a.c", "foo", "Initializes the widget.")
    st.set_call_map([CallEdge("a.c", "b.c", "foo", "bar")])
    st.commit()

    assert st.symbols_for_file("a.c")[0].doc == "Initializes the widget."
    edges = st.load_call_edges()
    assert len(edges) == 1 and edges[0].callee == "bar" and edges[0].dst_file == "b.c"
    st.close()


@pytest.mark.skipif(not clang_available(), reason="libclang not installed")
def test_clang_extracts_functions_and_call_edges(tmp_path):
    (tmp_path / "b.c").write_text("int bar(void) { return 1; }\n")
    (tmp_path / "a.c").write_text(
        "int bar(void);\nint foo(void) { return bar() + bar(); }\n"
    )
    ua = parse_file(tmp_path / "a.c", tmp_path)
    ub = parse_file(tmp_path / "b.c", tmp_path)
    assert ua.parsed and ub.parsed
    assert any(f.name == "foo" for f in ua.functions)
    assert any(f.name == "bar" for f in ub.functions)
    assert any(c.callee == "bar" for c in ua.calls)

    defs = build_definition_table(ua.functions + ub.functions)
    edges = resolve_calls(ua.calls + ub.calls, defs)
    assert any(e.src_file.endswith("a.c") and e.dst_file.endswith("b.c") for e in edges)
