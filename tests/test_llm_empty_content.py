"""A reasoning model emits `reasoning_content` before `content`. If the output budget
runs out first, the call returns HTTP 200 with EMPTY content and finish_reason="length" —
and every secagent caller reads "" as "nothing to report": blank summaries, zero scan
findings on genuinely buggy code. Observed on a live gemma-4 server during an agentic
test run. The client must notice and retry rather than pass the emptiness along.
"""

from __future__ import annotations

import json

import httpx

from .conftest import mock_client


def test_client_retries_empty_content_with_a_bigger_budget():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["max_tokens"])
        if len(calls) == 1:  # first attempt: the whole budget went to reasoning
            return httpx.Response(200, json={"choices": [
                {"message": {"role": "assistant", "content": ""},
                 "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"role": "assistant", "content": "REAL ANSWER"},
             "finish_reason": "stop"}]})

    client = mock_client(handler)
    try:
        r = client.chat([{"role": "user", "content": "hi"}], max_tokens=100)
    finally:
        client.close()
    assert r.content == "REAL ANSWER"
    assert len(calls) == 2 and calls[1] > calls[0]   # retried with a LARGER budget


def test_client_does_not_retry_a_normal_empty_stop():
    """finish_reason='stop' with empty content is the model's real answer, not truncation
    — retrying would double the cost of every such call."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"choices": [
            {"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]})

    client = mock_client(handler)
    try:
        client.chat([{"role": "user", "content": "hi"}], max_tokens=100)
    finally:
        client.close()
    assert len(calls) == 1


def test_content_present_never_retries():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"choices": [
            {"message": {"role": "assistant", "content": "fine"}, "finish_reason": "length"}]})

    client = mock_client(handler)
    try:
        assert client.chat([{"role": "user", "content": "hi"}]).content == "fine"
    finally:
        client.close()
    assert len(calls) == 1
