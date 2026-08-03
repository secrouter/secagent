"""JSON affordances return valid JSON on their empty/not-found paths (not a bare string),
so a structured consumer's json.loads never crashes. Tier-3 review item."""

from __future__ import annotations

import json

import pytest

from secagent.affordances import queries
from secagent.affordances.models import TypeRecord
from secagent.affordances.store import AffordanceStore


@pytest.fixture
def store(tmp_path):
    s = AffordanceStore(tmp_path, ".secagent")
    yield s
    s.close()


def _assert_note(out: str, needle: str) -> None:
    """The output parses as JSON, is the empty-result shape, and keeps the guidance."""
    data = json.loads(out)  # must not raise
    assert isinstance(data, dict) and "note" in data
    assert needle in data["note"]


def test_summary_empty_is_json(store):
    # Wording is the unknown-path guidance now; this test guards the JSON SHAPE.
    _assert_note(queries.summary(store, "nope.py"), "not in the index")


def test_find_symbol_empty_is_json(store):
    _assert_note(queries.find_symbol(store, "zzz_nothing"), "No symbols matching")


def test_functions_empty_is_json(store):
    _assert_note(queries.functions(store, "nope.c"), "not in the index")


def test_callers_empty_is_json(store):
    _assert_note(queries.callers(store, "zzz_nothing"), "No callers found")


def test_types_empty_and_no_match_are_json(store):
    _assert_note(queries.types(store), "No type graph indexed")
    store.set_types([TypeRecord("pkg.Foo", "class", "a.cs", 1, [], [])])
    store.commit()
    _assert_note(queries.types(store, "zzz_nothing"), "No types matching")


def test_success_paths_still_return_their_shapes(store):
    """The normalization must not change the populated shapes: list for find_symbol,
    object for a real summary."""
    from secagent.affordances.models import FileRecord, FileSummary, Symbol

    store.upsert_file(
        FileRecord(path="a.py", language="Python", size=1, sha256="x", loc=1, n_symbols=1),
        FileSummary(path="a.py", purpose="does a thing"),
        [Symbol("do_it", "function", "a.py", 1, "def do_it()", "", "", "")],
    )
    store.commit()
    assert isinstance(json.loads(queries.find_symbol(store, "do_it")), list)
    summ = json.loads(queries.summary(store, "a.py"))
    assert isinstance(summ, dict) and summ["purpose"] == "does a thing"


# --- unknown path vs empty result --------------------------------------------
# devJ (run 5): pi's built-in read/bash tools are relative to the CWD while the secagent
# tools are relative to the repo root. A caller who used the bash-correct path got
# "No functions indexed for 'gps/src/unicore.h'" — identical to a real empty result — so
# the model concluded secagent had no data and silently abandoned it for grep. The final
# answer was still right, so nothing surfaced the mistake.

def _repo(store):
    from secagent.affordances.models import FileRecord, FileSummary, Symbol
    store.upsert_file(
        FileRecord(path="src/unicore.cpp", language="C++", size=1, sha256="a", loc=1,
                   n_symbols=1),
        FileSummary(path="src/unicore.cpp", purpose="parser"),
        [Symbol("run", "function", "src/unicore.cpp", 1, "void run()")],
    )
    store.upsert_file(
        FileRecord(path="src/crc.h", language="C++", size=1, sha256="b", loc=1,
                   n_symbols=0),
        FileSummary(path="src/crc.h", purpose="header"), [],
    )
    store.commit()


def test_wrong_path_base_is_named_and_a_correction_suggested(store):
    _repo(store)
    note = json.loads(queries.functions(store, "gps/src/unicore.cpp"))["note"]
    assert "not in the index" in note
    assert "repo-relative" in note                      # explains the mistake
    assert "'src/unicore.cpp'" in note                  # suggests the fix


def test_indexed_file_with_no_functions_says_so_distinctly(store):
    _repo(store)
    note = json.loads(queries.functions(store, "src/crc.h"))["note"]
    assert "indexed but defines no functions" in note
    assert "not in the index" not in note               # a different situation


def test_unknown_path_without_a_match_points_at_search(store):
    _repo(store)
    note = json.loads(queries.functions(store, "totally/made/up.c"))["note"]
    assert "not in the index" in note and "search" in note


def test_summary_reports_an_unknown_path_the_same_way(store):
    _repo(store)
    note = json.loads(queries.summary(store, "gps/src/crc.h"))["note"]
    assert "not in the index" in note and "'src/crc.h'" in note


def test_known_paths_still_answer_normally(store):
    _repo(store)
    assert isinstance(json.loads(queries.functions(store, "src/unicore.cpp")), list)
    assert json.loads(queries.summary(store, "src/crc.h"))["purpose"] == "header"
