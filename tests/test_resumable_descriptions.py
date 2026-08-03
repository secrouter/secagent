"""The expensive half of a docs build must survive being killed.

A docs build makes one model call per function and was measured running 28 minutes on a
20-file repository without producing a page. The reasonable response to that is to kill
it — which is only reasonable if the work already done is kept.

It is: each description is written to the content-addressed cache the moment it lands, so
a re-run pays nothing for what finished and a killed run resumes exactly where it stopped.
That behaviour is load-bearing and was undocumented, so it is pinned here — it would be
easy to lose in a refactor that batched cache writes to the end, and the loss would be
invisible until someone waited out a long run twice.
"""

from __future__ import annotations

import httpx

from secagent.affordances.cache import ContentCache
from secagent.affordances.file_summary import describe_functions
from secagent.affordances.models import Symbol

from .conftest import make_chat_response, mock_client

SYMS = [Symbol(f"fn{i}", "function", "a.c", 1, "", "", "", "") for i in range(6)]


def _counting_client(counter):
    def handler(request):
        counter["n"] += 1
        return httpx.Response(200, json=make_chat_response(content="Does a thing."))
    return mock_client(handler)


def _describe(symbols, cache, counter):
    llm = _counting_client(counter)
    try:
        return describe_functions("a.c", "int x;", symbols, llm=llm,
                                  cache=cache, content_sha="abc")
    finally:
        llm.close()


def test_a_second_run_costs_nothing(tmp_path):
    cache = ContentCache(tmp_path / "c")
    counter = {"n": 0}

    first = _describe(SYMS, cache, counter)
    assert len(first) == 6 and counter["n"] == 6

    counter["n"] = 0
    second = _describe(SYMS, cache, counter)
    assert second == first
    assert counter["n"] == 0, "re-ran work that was already cached"


def test_a_killed_run_resumes_where_it_stopped(tmp_path):
    """The realistic case: someone gives up on a slow build and starts it again."""
    cache = ContentCache(tmp_path / "c")
    counter = {"n": 0}

    _describe(SYMS[:3], cache, counter)          # the part that finished before the kill
    assert counter["n"] == 3

    counter["n"] = 0
    out = _describe(SYMS, cache, counter)
    assert len(out) == 6
    assert counter["n"] == 3, "paid again for descriptions that already existed"


def test_changed_content_is_not_served_from_the_old_cache(tmp_path):
    """Resumption must not become staleness: a different file version is different work."""
    cache = ContentCache(tmp_path / "c")
    counter = {"n": 0}
    _describe(SYMS, cache, counter)

    counter["n"] = 0
    llm = _counting_client(counter)
    try:
        describe_functions("a.c", "int y;", SYMS, llm=llm, cache=cache,
                           content_sha="DIFFERENT")
    finally:
        llm.close()
    assert counter["n"] == 6
