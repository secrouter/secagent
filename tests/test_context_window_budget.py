"""The configured context window silently caps everything downstream.

`llm.context_window` defaults to 8192 — a Gemma 2 figure. Pointed at a local server
actually serving 117,976 tokens, secagent used 6% of the context available, and (much
worse) the empty-content retry from #70 derives its ceiling from the same setting: at
8192 the retry could never exceed 4096 tokens, while a reasoning model measurably needs
13-15k output tokens before it emits a single character. So the recovery path for
reasoning models was capped below the point of usefulness by a default nobody had cause
to look at, and `scan` reported every file as unanalysable.

Nothing surfaced any of this: not an error, just a confident under-use.
"""

from __future__ import annotations

import json

import httpx

from secagent.config import Settings
from secagent.doctor import check_llm_context_window, probe_context_window

from .conftest import mock_client


def _empty_then_answer(calls: list[int]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["max_tokens"])
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [
                {"message": {"role": "assistant", "content": ""},
                 "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"role": "assistant", "content": "ANSWER"},
             "finish_reason": "stop"}]})
    return handler


def test_retry_jumps_straight_to_real_headroom():
    """One jump, not a ladder: each step is another full round trip (85s+ measured) and
    can still land short. The target clears the measured 13-15k reasoning cost outright."""
    calls: list[int] = []
    client = mock_client(_empty_then_answer(calls))
    client.config.context_window = 117976
    try:
        r = client.chat([{"role": "user", "content": "hi"}], max_tokens=1024)
    finally:
        client.close()
    assert r.content == "ANSWER"
    assert calls[1] >= 16000, "must clear what the model measurably needs"


def test_retry_ceiling_scales_with_the_configured_window():
    """The regression: with an 8192 window the retry tops out at 4096 — below what a
    reasoning model needs, so the recovery path cannot recover."""
    calls: list[int] = []
    client = mock_client(_empty_then_answer(calls))
    client.config.context_window = 8192
    try:
        client.chat([{"role": "user", "content": "hi"}], max_tokens=1024)
    finally:
        client.close()
    assert calls[1] == 4096

    calls2: list[int] = []
    client2 = mock_client(_empty_then_answer(calls2))
    client2.config.context_window = 262144
    try:
        client2.chat([{"role": "user", "content": "hi"}], max_tokens=1024)
    finally:
        client2.close()
    # A bigger window must buy a materially bigger retry — but only up to the point where
    # more headroom stops buying recoveries and starts buying runaway wall time.
    assert calls2[1] >= calls[1] * 4
    assert calls2[1] <= 32768


def test_no_retry_when_the_window_cannot_offer_more(caplog):
    """Nothing to escalate to — but the user must be told which knob is binding."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={"choices": [
            {"message": {"role": "assistant", "content": ""},
             "finish_reason": "length"}]})

    client = mock_client(handler)
    client.config.context_window = 4096
    try:
        with caplog.at_level("WARNING"):
            client.chat([{"role": "user", "content": "hi"}], max_tokens=2048)
    finally:
        client.close()
    assert len(calls) == 1, "no point retrying at a budget that is not larger"
    assert "llm.context_window" in caplog.text, \
        "must name the setting that is actually binding, not one that cannot help"


# --- discovery ----------------------------------------------------------------

def _settings(model="m", window=8192) -> Settings:
    s = Settings()
    s.llm.base_url = "http://server:11434/v1"
    s.llm.model = model
    s.llm.context_window = window
    return s


def test_probes_lm_studio_shape(monkeypatch):
    def fake_get(url, **kw):
        assert url == "http://server:11434/api/v0/models"
        return httpx.Response(200, json={"data": [
            {"id": "other", "max_context_length": 999, "loaded_context_length": 999},
            {"id": "m", "max_context_length": 262144, "loaded_context_length": 117976},
        ]})
    monkeypatch.setattr(httpx, "get", fake_get)
    served, source = probe_context_window(_settings())
    # The LOADED length, not the maximum: a model served at 118k cannot use 262k.
    assert served == 117976 and "loaded" in source


def test_probes_ollama_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"model_info": {"gemma3.context_length": 131072}}))
    served, _ = probe_context_window(_settings())
    assert served == 131072


def test_silent_when_the_server_says_nothing(monkeypatch):
    """Plenty of endpoints do not advertise a window. That is not a failure."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404))
    served, _ = probe_context_window(_settings())
    assert served is None
    check = check_llm_context_window(_settings(), probe=True)
    assert check.ok


# --- the doctor check ---------------------------------------------------------

def _serving(monkeypatch, tokens):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={
        "data": [{"id": "m", "loaded_context_length": tokens}]}))


def test_reports_a_window_left_far_below_what_is_served(monkeypatch):
    """The live case: 8192 configured, 117976 served."""
    _serving(monkeypatch, 117976)
    check = check_llm_context_window(_settings(window=8192), probe=True)
    assert not check.ok
    assert "117976" in check.detail
    assert "SECAGENT_LLM__CONTEXT_WINDOW=117976" in check.detail, "say exactly how to fix it"


def test_reports_a_window_larger_than_the_server_allows(monkeypatch):
    """The opposite error is worse — prompts overflow at the server."""
    _serving(monkeypatch, 8192)
    check = check_llm_context_window(_settings(window=131072), probe=True)
    assert not check.ok and check.severity == "error"


def test_quiet_when_the_window_is_set_sensibly(monkeypatch):
    _serving(monkeypatch, 117976)
    assert check_llm_context_window(_settings(window=117976), probe=True).ok


# --- first-attempt budget -----------------------------------------------------

def test_scan_asks_for_a_budget_a_reasoning_model_can_finish_inside():
    """Following max_output_tokens (1024) guaranteed a wasted call plus a retry on every
    file. A generous cap is free — the model stops when done — so ask up front."""
    from secagent.agents.scan.agent import _default_budget

    cfg = Settings().llm
    cfg.context_window = 117976
    cfg.max_output_tokens = 1024
    assert _default_budget(cfg) >= 16000, "must clear the measured 13-15k reasoning cost"


def test_scan_budget_stays_inside_a_small_window():
    """It scales with the window; it must not ask a small deployment for more than it has."""
    from secagent.agents.scan.agent import _default_budget

    cfg = Settings().llm
    cfg.context_window = 8192
    cfg.max_output_tokens = 1024
    assert _default_budget(cfg) <= 8192 // 2


def test_an_explicit_max_output_tokens_is_still_honoured():
    from secagent.agents.scan.agent import _default_budget

    cfg = Settings().llm
    cfg.context_window = 8192
    cfg.max_output_tokens = 4000
    assert _default_budget(cfg) >= 4000


# --- the context block must actually use the window ---------------------------

def test_a_bigger_window_yields_a_bigger_context_block(tmp_path):
    """`for_query` capped file blocks at a fixed 12, which quietly became the ONLY
    constraint: against a server serving 117,976 tokens, a 14x larger window grew the
    assembled block by 7% because both runs stopped at twelve files. The budget is what
    should decide."""
    from secagent.affordances.models import FileRecord, FileSummary
    from secagent.affordances.retrieval import ContextBuilder
    from secagent.affordances.store import AffordanceStore

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        for i in range(80):
            path = f"src/mod{i}.py"
            store.upsert_file(
                FileRecord(path=path, language="Python", size=1, sha256=path, loc=1,
                           n_symbols=0),
                FileSummary(path=path,
                            purpose=f"handles widget routing for shard {i}; " * 20),
                [],
            )
        store.commit()

        # Budgets chosen so the BUDGET binds in both cases, not the ceiling — otherwise
        # this passes without testing anything (the first version did exactly that).
        small = ContextBuilder(store, 900).for_query("widget routing")
        large = ContextBuilder(store, 12000).for_query("widget routing")
        assert 0 < small.count("### ") < 60

        assert large.count("### ") > small.count("### ") * 2, \
            "a larger budget must buy more context, not the same fixed dozen files"
    finally:
        store.close()


def test_the_ceiling_is_configurable_and_respected(tmp_path):
    """Bounded, still: the point is the smallest context that answers the task, so a
    very large window must not turn a focused answer into a repository dump."""
    from secagent.affordances.models import FileRecord, FileSummary
    from secagent.affordances.retrieval import ContextBuilder
    from secagent.affordances.store import AffordanceStore

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        for i in range(80):
            path = f"src/mod{i}.py"
            store.upsert_file(
                FileRecord(path=path, language="Python", size=1, sha256=path, loc=1,
                           n_symbols=0),
                FileSummary(path=path, purpose=f"handles widget routing for shard {i}"),
                [],
            )
        store.commit()
        out = ContextBuilder(store, 400000, max_files=5).for_query("widget routing")
        assert out.count("### ") == 5
    finally:
        store.close()


def test_retry_is_bounded_so_a_runaway_model_cannot_burn_the_window():
    """A larger cap is free only while the model converges. Measured: a 3894-byte header
    consumed a 58988-token budget over 10.5 minutes and still produced nothing, so the
    extra headroom bought no recovery and cost minutes."""
    calls: list[int] = []
    client = mock_client(_empty_then_answer(calls))
    client.config.context_window = 262144        # ceiling would be 131072
    try:
        client.chat([{"role": "user", "content": "hi"}], max_tokens=1024)
    finally:
        client.close()
    assert calls[1] <= 32768, "retry must not scale to the whole window"
    assert calls[1] >= 16000, "...but must still clear the measured 13-15k reasoning cost"


# --- the defaults themselves --------------------------------------------------

def test_defaults_target_a_modern_local_model():
    """8192 was a Gemma 2 figure. Gemma 3 (4B+) is 128k and Gemma 4 reaches 256k, and
    the old default silently capped the empty-content retry at 4096 — below the measured
    13-15k a reasoning model needs before it emits anything."""
    cfg = Settings().llm
    assert cfg.context_window >= 131072
    assert cfg.max_output_tokens > 12288, "must clear the budgets measured to return EMPTY"


def test_a_window_slightly_above_what_is_loaded_is_not_an_error(monkeypatch):
    """Only `context_budget_ratio` of the window is ever filled, so a default a little
    above what a server happens to have loaded is harmless. Erroring on it would fire on
    a working setup — and a check that cries wolf stops being read."""
    _serving(monkeypatch, 117976)
    s = _settings(window=131072)
    assert s.llm.context_budget_tokens + s.llm.max_output_tokens < 117976
    assert check_llm_context_window(s, probe=True).ok


def test_a_window_that_would_genuinely_overflow_still_errors(monkeypatch):
    _serving(monkeypatch, 117976)
    check = check_llm_context_window(_settings(window=262144), probe=True)
    assert not check.ok and check.severity == "error"
