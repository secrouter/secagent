"""OpenAI-compatible chat client.

Works unchanged against llama.cpp ``llama-server`` and vLLM. Supports native
tool-calling and degrades to a JSON tool protocol (see ``llm.tooling``) for servers
without it. TLS uses the system OpenSSL trust store (FIPS-friendly).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig
from ..secretval import SecretResolutionError, resolve_secret

log = logging.getLogger(__name__)

# Floor below which a retry ceiling is too small to help a reasoning model at all, used
# only to decide WHICH setting to name in the give-up message. Measured against a local
# Gemma-4, an ordinary "analyse this source" prompt spent 13-15k output tokens on
# `reasoning_content` before emitting a single character: 8k and 12k both returned EMPTY,
# 16k answered.
_REASONING_RETRY_FLOOR = 16384

# Upper bound on an escalated retry. Retrying at the whole half-window looked free —
# a reasoning model stops when it is done, so a larger cap normally buys no extra time.
# That holds right up until the model does NOT converge: measured, a 3894-byte header
# consumed a 58988-token budget and 10.5 minutes before giving up. Neither observed
# runaway was rescued by the larger budget, so the headroom bought nothing and cost
# minutes. 2x the measured 13-15k need recovers the real cases and bounds the tail.
_REASONING_RETRY_CEILING = 32768


def _truncated(data: dict[str, Any]) -> bool:
    """True when the model stopped because it hit the output budget."""
    try:
        return data["choices"][0].get("finish_reason") == "length"
    except (KeyError, IndexError, TypeError):
        return False

# A chat message is a plain dict in OpenAI shape, e.g.
#   {"role": "system"|"user"|"assistant"|"tool", "content": str, ...}
ChatMessage = dict[str, Any]


class LLMError(RuntimeError):
    """Raised when the endpoint cannot satisfy a request after retries."""


@dataclass
class ToolSpec:
    """A tool the model may call. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """Thin, retrying client over ``/chat/completions``.

    A custom ``httpx.Client`` may be injected for testing (e.g. with a
    ``MockTransport``); otherwise one is created from the config.
    """

    def __init__(self, config: LLMConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self._owns_http = http is None
        # base_url MUST end with "/" and request paths MUST be relative (no leading
        # "/"), otherwise httpx drops the base path component (e.g. the "/v1" suffix).
        #
        # Authorization is deliberately NOT a fixed header here (contrast the older
        # behaviour, which baked `Bearer {config.api_key}` in once at construction).
        # `api_key` may be a `"!command"` (e.g. `"!secagent token"`, resolved per pi's
        # own `models.json` convention — see `secretval.py` and `secsso.py`) that must
        # be re-resolved on every request, not cached for the client's lifetime. See
        # `_auth_headers`, applied per-call in `_post_with_retry`.
        self._http = http or httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.request_timeout_s,
            verify=True,  # system OpenSSL / FIPS trust store
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        """``timeout`` bounds this whole call — every retry and the empty-content
        escalation included — not each HTTP request.

        A deadline has to live where it can actually interrupt. Enforcing one around a
        worker thread does not stop the request — Python cannot kill a thread — so the
        call runs to completion regardless and the "deadline" only changes when you stop
        looking. Bounding the transport is what actually ends it.

        It was previously passed to `httpx` as a PER-REQUEST timeout, which
        `_post_with_retry` then wrapped in a `max_retries` loop (default 3), each attempt
        getting a fresh full budget plus backoff — and the empty-content path below can
        issue a second full cycle. `scan.per_file_timeout_s = 300` therefore admitted
        ~1800s, while its own comment claimed a hard ceiling including the retry. A
        deadline computed once here and shared by every attempt is what makes that true.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_output_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools and self.config.native_tool_calls:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"

        data = self._post_with_retry("chat/completions", payload, deadline=deadline)
        resp = _parse_response(data)

        # Reasoning models emit `reasoning_content` BEFORE `content`. If the output budget
        # runs out first, the call succeeds (HTTP 200) with EMPTY content and
        # finish_reason="length" — and every caller reads "" as "nothing to report":
        # summaries come back blank, scans report zero findings on real bugs. Retry once
        # with a bigger budget rather than let that pass silently.
        if not resp.content and not resp.tool_calls and _truncated(data):
            budget = payload["max_tokens"]
            ceiling = self.config.context_window // 2
            # Escalate in one jump to a budget with real headroom over the measured
            # 13-15k reasoning cost, rather than stepping up in multiples (another full
            # 85s+ round trip per step, and it can still land short). Bounded rather than
            # "as much as the window allows": see _REASONING_RETRY_CEILING.
            retry_budget = min(max(budget * 8, _REASONING_RETRY_FLOOR),
                               _REASONING_RETRY_CEILING, ceiling)
            if retry_budget > budget:
                log.warning(
                    "LLM returned empty content with finish_reason='length' (%d token "
                    "budget consumed by reasoning); retrying with %d.", budget, retry_budget)
                payload["max_tokens"] = retry_budget
                resp = _parse_response(
                    self._post_with_retry("chat/completions", payload, deadline=deadline))
            if not resp.content and not resp.tool_calls:
                # Name the setting that is actually binding. The retry ceiling derives from
                # context_window, so a context_window left at a small default caps recovery
                # below what reasoning models need — measured at 13-15k output tokens for an
                # ordinary source-analysis prompt. Pointing at max_output_tokens in that
                # case sends people to tune a knob that cannot help.
                if ceiling <= _REASONING_RETRY_FLOOR:
                    log.warning(
                        "LLM produced no content even at a %d-token budget. The retry is "
                        "capped at %d by llm.context_window=%d; reasoning models routinely "
                        "need more than that before emitting any content. Set "
                        "llm.context_window to the window your server actually serves "
                        "(`secagent doctor --probe` reports it).",
                        payload["max_tokens"], ceiling, self.config.context_window)
                else:
                    log.warning(
                        "LLM produced no content even at a %d-token budget — the model may "
                        "be spending the whole budget on reasoning. Raise "
                        "llm.max_output_tokens.", payload["max_tokens"])
        return resp

    def _auth_headers(self) -> dict[str, str]:
        """Resolve ``config.api_key`` fresh (see the ``__init__`` note on why this is
        not cached) and wrap resolution failures as ``LLMError`` so the existing
        retry/backoff loop in ``_post_with_retry`` handles them uniformly alongside
        ordinary transport errors — a transient SecSSO/command hiccup gets the same
        bounded retries a transient network error would."""
        try:
            key = resolve_secret(self.config.api_key)
        except SecretResolutionError as exc:
            raise LLMError(f"could not resolve llm.api_key: {exc}") from exc
        return {"Authorization": f"Bearer {key}"}

    def _post_with_retry(self, path: str, payload: dict[str, Any],
                         deadline: float | None = None) -> dict[str, Any]:
        """``deadline`` is an absolute ``time.monotonic()`` instant, shared by every
        attempt AND by the caller's later attempts, so the whole `chat()` call is bounded
        by it rather than each request being bounded separately."""
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise LLMError(
                    f"LLM deadline exceeded after {attempt} attempt(s): {last_exc}"
                    if last_exc else "LLM deadline exceeded before the request was sent")
            try:
                headers = self._auth_headers()
                resp = (self._http.post(path, json=payload, headers=headers) if remaining is None
                        else self._http.post(path, json=payload, headers=headers,
                                             timeout=remaining))
                if resp.status_code >= 500:
                    raise LLMError(f"server {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, LLMError) as exc:
                last_exc = exc
                if attempt < self.config.max_retries - 1:
                    backoff = min(2**attempt, 8)
                    if deadline is not None:
                        # Sleeping past the deadline burns the caller's budget to no
                        # purpose — the next attempt would be refused on arrival anyway.
                        backoff = min(backoff, max(0.0, deadline - time.monotonic()))
                    time.sleep(backoff)
        raise LLMError(f"LLM request failed after {self.config.max_retries} attempts: {last_exc}")


def _parse_response(data: dict[str, Any]) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in response: {json.dumps(data)[:200]}")
    msg = choices[0].get("message", {})
    content = msg.get("content") or ""
    tool_calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        tool_calls.append(
            ToolCall(id=tc.get("id", fn.get("name", "call")), name=fn.get("name", ""),
                     arguments=args or {})
        )
    usage = data.get("usage", {})
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=choices[0].get("finish_reason", "stop"),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        raw=data,
    )
