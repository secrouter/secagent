"""Relevance ranking must survive a natural-language question.

Agents do not query with keywords, they query with sentences. Asking a PX4 GNSS repo
"how do I add a new Unicore message type to this parser?" ranked an unrelated protocol's
files first and put `unicore.h` 11th of 12 — dead last in the block. Every word in that
sentence except one appears in every driver, and raw term frequency let the filler
outvote the one term that identified the answer.

That block is fed to a model as ground truth, so the failure is not "a worse ordering":
it is a confident, plausible, wrong context — the defect class this whole suite exists
to catch.
"""

from __future__ import annotations

from secagent.affordances.models import FileRecord, FileSummary
from secagent.affordances.retrieval import ContextBuilder
from secagent.affordances.store import AffordanceStore

# Every driver in the repo describes itself with the same nouns; only the protocol name
# differs. That is the situation that broke ranking, so it is what the fixture builds.
_BOILERPLATE = ("parser for {p} protocol messages: decodes a message, checks the "
                "message type, and adds the new message to the parser output")

_PROTOCOLS = ["ubx", "sbf", "ashtech", "femtomes", "nmea", "emlid", "septentrio",
              "trimble", "novatel", "unicore"]


def _repo(store: AffordanceStore) -> None:
    for proto in _PROTOCOLS:
        for ext in ("h", "cpp"):
            path = f"src/{proto}.{ext}"
            # `ubx` is the biggest file in the real repo — it repeats the shared
            # vocabulary far more often than anyone else, which is exactly how it won.
            reps = 12 if proto == "ubx" else 1
            purpose = " ".join([_BOILERPLATE.format(p=proto)] * reps)
            store.upsert_file(
                FileRecord(path=path, language="C++", size=1, sha256=path, loc=1,
                           n_symbols=1),
                FileSummary(path=path, purpose=purpose),
                [],
            )
    store.commit()


def _rank(store: AffordanceStore, query: str) -> list[str]:
    return [s.path for s in ContextBuilder(store, 4000).rank_summaries(query)]


def test_sentence_query_finds_the_file_it_names(tmp_path):
    """The reported defect, verbatim."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo(store)
        ranked = _rank(store, "how do I add a new Unicore message type to this parser?")
        assert ranked[0].startswith("src/unicore."), \
            f"the query names Unicore; ranked first was {ranked[0]}"
        # Both halves of the pair must be near the top — a model asked to *add* a message
        # type needs the header as much as the implementation.
        assert {"src/unicore.h", "src/unicore.cpp"} <= set(ranked[:3])
    finally:
        store.close()


def test_a_large_file_does_not_win_on_volume(tmp_path):
    """`ubx` repeats the shared vocabulary 12x. Size is not relevance."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo(store)
        ranked = _rank(store, "how do I add a new Unicore message type to this parser?")
        assert "src/ubx.h" not in ranked[:2]
    finally:
        store.close()


def test_other_protocols_still_rank_for_their_own_names(tmp_path):
    """The fix must not simply bias toward one file: every protocol answers for itself."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo(store)
        for proto in ("ubx", "sbf", "nmea", "trimble"):
            ranked = _rank(store, f"where is the {proto} message type parsed?")
            assert ranked[0].startswith(f"src/{proto}."), \
                f"{proto} query ranked {ranked[0]} first"
    finally:
        store.close()


def test_words_common_to_every_file_do_not_decide_the_order(tmp_path):
    """A term in all N files carries no information; it must not shuffle the ranking.

    Guards the IDF weighting directly: padding a query with universal vocabulary should
    leave the outcome unchanged.
    """
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        _repo(store)
        bare = _rank(store, "unicore")
        padded = _rank(store, "parser message protocol decodes output unicore")
        assert bare[0] == padded[0] == "src/unicore.cpp"
    finally:
        store.close()
