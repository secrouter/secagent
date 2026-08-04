"""Tests for the LLM client, tokenizer, and tool-call fallback parsing."""

from __future__ import annotations

import httpx
import pytest

from secagent.llm.client import LLMError
from secagent.llm.tokenizer import TokenCounter, count_tokens
from secagent.llm.tooling import extract_final, extract_tool_call, render_tool_instructions

from .conftest import make_chat_response, mock_client, scripted_client


def test_tokenizer_heuristic_is_positive_and_monotonic():
    tc = TokenCounter()
    assert not tc.exact
    assert tc.count("") == 0
    short = tc.count("hello world")
    long = tc.count("hello world " * 50)
    assert short > 0
    assert long > short


def test_count_tokens_module_helper():
    assert count_tokens("def foo(): return 1") > 0


def test_count_messages_includes_overhead():
    tc = TokenCounter()
    n = tc.count_messages([{"role": "user", "content": "hi"}])
    assert n >= tc.count("hi")


def test_chat_parses_plain_content():
    client = scripted_client([make_chat_response(content="hello")])
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "hello"
    assert resp.tool_calls == []
    assert resp.prompt_tokens == 10


def test_chat_parses_native_tool_calls():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_structure", "arguments": '{"depth": 2}'},
        }
    ]
    client = scripted_client(
        [make_chat_response(tool_calls=tool_calls, finish_reason="tool_calls")]
    )
    resp = client.chat([{"role": "user", "content": "map it"}])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_structure"
    assert resp.tool_calls[0].arguments == {"depth": 2}


def test_chat_retries_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=make_chat_response(content="ok"))

    client = mock_client(handler, max_retries=3)
    resp = client.chat([{"role": "user", "content": "x"}])
    assert resp.content == "ok"
    assert attempts["n"] == 2


def test_chat_raises_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = mock_client(handler, max_retries=2)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "x"}])


def test_extract_tool_call_plain_json():
    tc = extract_tool_call('{"tool": "find_symbol", "arguments": {"name": "main"}}')
    assert tc is not None
    assert tc.name == "find_symbol"
    assert tc.arguments == {"name": "main"}


def test_extract_tool_call_fenced_with_prose():
    text = 'Sure, let me look.\n```json\n{"tool": "get_io_edges", "arguments": {}}\n```\n'
    tc = extract_tool_call(text)
    assert tc is not None
    assert tc.name == "get_io_edges"


def test_extract_tool_call_lenient_trailing_comma():
    tc = extract_tool_call('{"tool": "x", "arguments": {"a": 1,},}')
    assert tc is not None
    assert tc.arguments == {"a": 1}


def test_extract_final():
    assert extract_final('{"final": "all done"}') == "all done"
    assert extract_final('{"tool": "x", "arguments": {}}') is None


def test_render_tool_instructions_lists_tools():
    from secagent.llm.client import ToolSpec

    spec = ToolSpec("get_structure", "Map the repo", {"properties": {"depth": {}}})
    text = render_tool_instructions([spec])
    assert "get_structure(depth)" in text
    assert "final" in text


def test_the_call_deadline_covers_every_retry_not_each_request():
    """`chat(timeout=...)` is a deadline for the whole call, not a per-request timeout.

    It used to be handed straight to httpx per request, and `_post_with_retry` wrapped
    that in a `max_retries` loop (default 3) with backoff between attempts — so
    `scan.per_file_timeout_s = 300` admitted ~900s, and ~1800s once the empty-content
    escalation issued a second full cycle. The setting's own comment claimed a hard
    ceiling "INCLUDING the empty-content retry", which was false.

    The handler raises `httpx.ReadTimeout` because that is what the real transport raises
    on expiry; `httpx.MockTransport` does not enforce timeouts itself. What is under test
    is the arithmetic that shares one deadline across attempts, not httpx's own clock.
    """
    import time

    attempts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(time.monotonic())
        time.sleep(0.2)
        raise httpx.ReadTimeout("simulated transport timeout", request=request)

    llm = mock_client(handler, max_retries=3)
    started = time.monotonic()
    with pytest.raises(LLMError, match="deadline"):
        llm.chat([{"role": "user", "content": "x"}], timeout=0.5)
    elapsed = time.monotonic() - started

    # Unbounded, this is 0.2 + 1s backoff + 0.2 + 2s backoff + 0.2 = ~3.6s over 3 attempts.
    assert elapsed < 1.0, f"the 0.5s deadline bounded the call at {elapsed:.2f}s"
    assert len(attempts) < 3, "attempts must stop at the deadline, not at max_retries"


def test_the_empty_content_escalation_is_inside_the_same_deadline():
    """The retry-on-empty-content path issues a SECOND full `_post_with_retry` cycle. It
    shared the caller's timeout value, so it got its own fresh budget — the doubling on
    top of the retry multiplication. It must share the deadline instead, and refuse once
    the deadline has passed rather than start a new round of requests."""
    import time

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        time.sleep(0.3)
        return httpx.Response(200, json=make_chat_response(
            content="", finish_reason="length"))

    llm = mock_client(handler, max_retries=1)
    with pytest.raises(LLMError, match="deadline"):
        llm.chat([{"role": "user", "content": "x"}], max_tokens=64, timeout=0.25)

    assert calls == [1], "the escalation must not start a new request past the deadline"


# -- Authorization header resolution (llm.api_key: literal / $VAR / !command) -------
#
# api_key is resolved fresh on every request rather than baked into the client at
# construction, so a `"!secagent token"` value (see secretval.py / secsso.py) picks up
# a refreshed token on every call. These tests capture the actual outgoing header to
# prove that, not just that resolve_secret() works in isolation (covered by
# test_secretval.py already).


def test_auth_header_uses_literal_api_key_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler, api_key="literal-key-123")
    llm.chat([{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer literal-key-123"


def test_auth_header_resolves_env_var_api_key(monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_LLM_KEY", "from-env-456")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler, api_key="$SECAGENT_TEST_LLM_KEY")
    llm.chat([{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer from-env-456"


def test_auth_header_resolves_command_api_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler, api_key="!echo from-command-789")
    llm.chat([{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer from-command-789"


def test_auth_header_resolution_failure_raises_llmerror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the network when api_key is unresolvable")

    llm = mock_client(handler, api_key="$SECAGENT_TEST_LLM_KEY_DEFINITELY_UNSET", max_retries=1)
    with pytest.raises(LLMError, match="could not resolve llm.api_key"):
        llm.chat([{"role": "user", "content": "hi"}])


def test_auth_header_is_re_resolved_on_every_request_not_cached_at_construction(tmp_path):
    """Proves resolution happens per `chat()` call rather than once in __init__: a
    command whose output changes between invocations must produce a different header
    on a second call against the SAME LLMClient instance."""
    counter_file = tmp_path / "counter"
    counter_file.write_text("0")
    # POSIX shell one-liner: read, increment, print, persist.
    command = (
        f"!n=$(cat {counter_file}); n=$((n + 1)); "
        f"echo $n > {counter_file}; printf 'token-%s' \"$n\""
    )
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler, api_key=command)
    llm.chat([{"role": "user", "content": "first"}])
    llm.chat([{"role": "user", "content": "second"}])

    assert seen == ["Bearer token-1", "Bearer token-2"]
