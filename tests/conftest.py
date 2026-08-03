"""Shared test fixtures: a scriptable mock OpenAI-compatible endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from secagent.config import LLMConfig
from secagent.llm.client import LLMClient


def make_chat_response(content: str = "", tool_calls: list[dict] | None = None,
                       finish_reason: str = "stop") -> dict:
    """Build an OpenAI chat-completion response body."""
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def mock_client(handler: Callable[[httpx.Request], httpx.Response], **cfg) -> LLMClient:
    """An LLMClient whose HTTP calls are served by ``handler``."""
    config = LLMConfig(base_url="http://mock/v1", max_retries=cfg.pop("max_retries", 2), **cfg)
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url=config.base_url, transport=transport)
    return LLMClient(config, http=http)


def scripted_client(responses: list[dict], **cfg) -> LLMClient:
    """An LLMClient that returns ``responses`` in order, one per request."""
    calls = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["i"], len(responses) - 1)
        calls["i"] += 1
        return httpx.Response(200, json=responses[i])

    client = mock_client(handler, **cfg)
    client._call_count = calls  # type: ignore[attr-defined]
    return client


@pytest.fixture
def echo_tool_response():
    return make_chat_response


@pytest.fixture
def captured_requests():
    """Returns (client_factory, store) — store collects request JSON bodies."""
    store: list[dict] = []

    def factory(response: dict, **cfg) -> LLMClient:
        def handler(request: httpx.Request) -> httpx.Response:
            store.append(json.loads(request.content))
            return httpx.Response(200, json=response)

        return mock_client(handler, **cfg)

    return factory, store
