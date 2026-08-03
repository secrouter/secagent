"""Model-evaluation support: model-aware summary cache, --refresh, clang parse cache,
and the per-model summaries manifest."""

from __future__ import annotations

import httpx

from secagent.affordances import queries
from secagent.affordances.cache import ContentCache
from secagent.affordances.clang_ast import (
    CallSite,
    FuncDef,
    ParsedUnit,
    unit_from_cache,
    unit_to_cache,
)
from secagent.affordances.file_summary import summarize_file
from secagent.affordances.models import FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore

from .conftest import make_chat_response, mock_client

_SRC = "// a file\nint main(void) { return 0; }\n"


def _counting_llm(counter: dict, model: str):
    def handler(_req: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json=make_chat_response(content="does a thing."))
    return mock_client(handler, model=model)


def test_summary_cache_is_model_aware(tmp_path):
    cache = ContentCache(tmp_path / "cache")
    n = {"n": 0}
    a = _counting_llm(n, "model-A")
    b = _counting_llm(n, "model-B")

    summarize_file("f.c", _SRC, "C", [], llm=a, cache=cache, content_sha="sha-1")
    assert n["n"] == 1                      # first run: model A called
    summarize_file("f.c", _SRC, "C", [], llm=a, cache=cache, content_sha="sha-1")
    assert n["n"] == 1                      # same model + content: cache hit, no call
    summarize_file("f.c", _SRC, "C", [], llm=b, cache=cache, content_sha="sha-1")
    assert n["n"] == 2                      # different model: regenerated


def test_refresh_summaries_forces_regeneration(tmp_path):
    cache = ContentCache(tmp_path / "cache")
    n = {"n": 0}
    a = _counting_llm(n, "model-A")

    summarize_file("f.c", _SRC, "C", [], llm=a, cache=cache, content_sha="sha-1")
    summarize_file("f.c", _SRC, "C", [], llm=a, cache=cache, content_sha="sha-1")
    assert n["n"] == 1                      # cached
    summarize_file("f.c", _SRC, "C", [], llm=a, cache=cache, content_sha="sha-1",
                   refresh_summaries=True)
    assert n["n"] == 2                      # forced regen for the same model


def test_clang_unit_cache_roundtrip():
    unit = ParsedUnit(
        functions=[FuncDef("f", "int f(void)", "a.c", 1)],
        calls=[CallSite("f", "g", "a.c", 2)],
        parsed=True,
        errors=3,
    )
    back = unit_from_cache(unit_to_cache(unit))
    assert back.parsed is True
    assert back.errors == 3
    assert back.functions[0].name == "f" and back.functions[0].signature == "int f(void)"
    assert back.calls[0].callee == "g" and back.calls[0].line == 2


def test_summaries_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = AffordanceStore(repo, ".secagent")
    try:
        store.upsert_file(
            FileRecord("a.c", "C", 10, "sha", 5, 1),
            FileSummary(path="a.c", purpose="does A"),
            [Symbol("f", "function", "a.c", 1, "int f(void)")],
        )
        store.set_symbol_doc("a.c", "f", "frobs the widget")
        store._set_meta("summary_model", "model-X")
        store.commit()
        m = queries.summaries_manifest(store)
    finally:
        store.close()

    assert m["model"] == "model-X"
    assert m["files"]["a.c"] == "does A"
    assert m["functions"]["a.c"]["f"] == "frobs the widget"


def test_raw_cache_dump(tmp_path):
    import json

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.cache.put(ContentCache.make_key("sha1", "v1", "model-A"), {"purpose": "does A"})
        store.cache.put(ContentCache.make_key("sha2", "fn-v1:foo", "model-A"), {"doc": "frobs"})
        data = json.loads(queries.raw_cache(store))
    finally:
        store.close()

    assert data["counts"] == {"purpose": 1, "doc": 1, "other": 0, "total": 2}
    assert any(e["purpose"] == "does A" for e in data["purposes"])
    assert any(e["doc"] == "frobs" for e in data["function_docs"])
