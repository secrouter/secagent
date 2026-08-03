"""Generated prose can be checked against the index that already knows the truth.

Every LLM-written artifact names paths and symbols, and secagent has indexed every real
one. A reference that does not resolve is not a matter of taste — it is a fact the model
got wrong, catchable without another model call.

The evaluation runs were full of exactly this: a triage note asserting "the code
explicitly checks its value" about code that never does; a docs page attributing Kafka to
a component with no broker; tests generated from the first 16% of a file and free to
invent the rest. Each was caught by a human reading source. Each was mechanically
checkable.
"""

from __future__ import annotations

from secagent.affordances import grounding
from secagent.affordances.models import FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore


def _store(tmp_path):
    store = AffordanceStore(tmp_path, ".secagent")
    store.upsert_file(
        FileRecord(path="src/unicore.cpp", language="C++", size=1,
                   sha256="a", loc=10, n_symbols=2),
        FileSummary(path="src/unicore.cpp", purpose="unicore parser"),
        # The class itself IS indexed, exactly as the real indexer stores it. Omitting
        # it made `test_an_invented_symbol_is_flagged` pass for the wrong reason: the
        # old either-half rule would have verified a fake method against a real class,
        # and the fixture hid that. An evaluation agent caught it.
        [Symbol("UnicoreParser", "class", "src/unicore.cpp", 1, "", "", "",
                "UnicoreParser"),
         Symbol("parseChar", "method", "src/unicore.cpp", 10, "", "", "",
                "UnicoreParser::parseChar"),
         Symbol("crc32", "function", "src/unicore.cpp", 40, "", "", "", "")],
    )
    store.commit()
    return store


def test_a_real_path_and_symbol_verify(tmp_path):
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "See `UnicoreParser::parseChar` in src/unicore.cpp.")
        assert g.ok
        assert "src/unicore.cpp" in g.verified_paths
        assert "UnicoreParser::parseChar" in g.verified_symbols
        assert g.note() == ""
    finally:
        store.close()


def test_an_invented_path_is_flagged(tmp_path):
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "Configuration lives in src/kafka_client.cpp.")
        assert not g.ok
        assert "src/kafka_client.cpp" in g.unverified_paths
        assert "UNVERIFIED" in g.note()
    finally:
        store.close()


def test_an_invented_symbol_is_flagged(tmp_path):
    """The testgen failure mode: asserting against API the model never saw.

    The class is real and indexed; only the member is invented. Accepting a match on
    either half of `Class::member` let every such hallucination through.
    """
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "Call `UnicoreParser::parseBestnavaFrame` first.")
        assert not g.ok
        assert "UnicoreParser::parseBestnavaFrame" in g.unverified_symbols
    finally:
        store.close()


def test_a_bare_filename_still_resolves(tmp_path):
    """Generated prose cites `unicore.cpp` as often as the full path."""
    store = _store(tmp_path)
    try:
        assert grounding.check(store, "unicore.cpp handles it.").ok
    finally:
        store.close()


def test_prose_with_no_references_is_not_flagged(tmp_path):
    """A signal that fires on ordinary sentences is one nobody reads."""
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "This module parses incoming messages and validates "
                                   "the checksum before dispatching them.")
        assert g.ok and g.checked == 0
    finally:
        store.close()


def test_unmarked_prose_words_are_not_treated_as_symbols(tmp_path):
    """Only backticked identifiers count. Free prose is full of symbol-shaped words, and
    guessing produces false alarms that drown the real ones."""
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "The Parser reads a Buffer and returns a Result.")
        assert g.ok and not g.unverified_symbols
    finally:
        store.close()


def test_urls_and_docs_are_not_treated_as_repo_paths(tmp_path):
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "See docs at example.com and README.md for details.")
        assert g.ok, g.unverified_paths
    finally:
        store.close()


def test_extraction_finds_both_kinds():
    paths, symbols = grounding.extract_references(
        "In src/a.c the `do_thing` helper calls `Ns::other`.")
    assert paths == {"src/a.c"}
    assert symbols == {"do_thing", "Ns::other"}


# --- generated CODE, not prose ------------------------------------------------
# The first version only looked at backticked names, so it examined NOTHING in a .cpp
# file — the artifact UC5 actually produces — and returned ok. Found by an evaluation
# agent feeding it a synthetic test file with three fabricated calls.

def test_a_fabricated_qualified_call_in_source_is_flagged(tmp_path):
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "UnicoreParser p;\nUnicoreParser::alsoFake(1);\n")
        assert not g.ok
        assert "UnicoreParser::alsoFake" in g.unverified_symbols
    finally:
        store.close()


def test_a_real_qualified_call_in_source_verifies(tmp_path):
    store = _store(tmp_path)
    try:
        assert grounding.check(store, "UnicoreParser::parseChar(c);").ok
    finally:
        store.close()


def test_calls_on_types_we_do_not_own_are_not_flagged(tmp_path):
    """Generated tests call the framework and the standard library. Flagging those would
    bury the real findings — a check that cries wolf is one nobody reads."""
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "std::vector<int> v;\nASSERT_EQ(1, 1);\n"
                                   "testing::InitGoogleTest(&argc, argv);")
        assert g.ok, g.unverified_symbols
    finally:
        store.close()


def test_ungroundable_member_calls_are_counted_not_hidden(tmp_path):
    """`p.foo()` needs type inference we do not do. Counting them keeps `ok` from being
    read as "everything was checked"."""
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "p.andThis();\nq->orThat();\n")
        assert g.unchecked == 2
        assert g.to_dict()["unchecked_member_calls"] == 2
    finally:
        store.close()


# --- the checker must not cry wolf on dotted attribute syntax -----------------
# Matching "any short alphabetic suffix" as a file extension made every attribute access
# look like a path. On Python that was ~100% false positive — `click.Abort`, `ctx.color`,
# `obj.value` all reported as paths not in the index. A checker that is always wrong on a
# whole language is worse than none: it trains people to ignore it.

def test_attribute_access_is_not_a_path():
    paths, _ = grounding.extract_references(
        "raise click.Abort() if ctx.color else obj.value")
    assert paths == set()


def test_method_calls_are_not_paths():
    paths, _ = grounding.extract_references("self.assertEqual(x.y, z.w)")
    assert paths == set()


def test_real_source_paths_still_extract():
    paths, _ = grounding.extract_references(
        "see src/click/core.py, utils.py, src/unicore.cpp and crc.h")
    assert paths == {"src/click/core.py", "utils.py", "src/unicore.cpp", "crc.h"}


def test_python_prose_produces_no_false_findings(tmp_path):
    """End to end: the whole point is that a clean artifact reports clean."""
    store = _store(tmp_path)
    try:
        g = grounding.check(store, "The runner raises click.Abort when ctx.color is set.")
        assert g.ok, g.unverified_paths
    finally:
        store.close()
