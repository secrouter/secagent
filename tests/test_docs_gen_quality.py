"""Docs-generation quality (from the cFS docs review).

- Truncated prose (the model hit its output-token cap) is trimmed to the last complete
  sentence instead of being shipped as a mid-word fragment (e.g. "…tools/cfs-cosmos-").
- The per-component file listing omits non-code asset/binary ("Other") files (PDFs,
  LICENSE, .mk, …) that previously appeared as "Other file (N bytes)" noise.
"""

from __future__ import annotations

from secagent.affordances.models import Component
from secagent.agents.docs.outline import _components_page, _prose
from secagent.llm.client import LLMResponse


class _Counter:
    def count(self, text: str) -> int:
        return len(text.split())


class _LLM:
    def __init__(self, content: str, finish_reason: str) -> None:
        self._content = content
        self._fr = finish_reason

    def chat(self, messages, max_tokens=None):  # noqa: ANN001, ARG002
        return LLMResponse(content=self._content, finish_reason=self._fr)


def test_truncated_prose_trimmed_to_last_sentence():
    truncated = "The system has two parts. An endpoint is served by tools/cfs-cosmos-"
    out = _prose(_LLM(truncated, "length"), _Counter(), 1000,
                 "Describe.", "ctx", fallback="FB")
    assert out == "The system has two parts."  # the dangling fragment is dropped


def test_complete_prose_kept_verbatim():
    full = "It does one clear thing."
    out = _prose(_LLM(full, "stop"), _Counter(), 1000, "Describe.", "ctx", fallback="FB")
    assert out == full


def test_truncated_with_no_sentence_falls_back():
    out = _prose(_LLM("no boundary here at all", "length"), _Counter(), 1000,
                 "Describe.", "ctx", fallback="FB")
    assert out == "FB"


def test_component_listing_omits_other_assets():
    comp = Component(
        name="apps/fm", path="apps/fm", kind="package",
        files=["apps/fm/fsw/src/fm_app.c", "apps/fm/LICENSE",
               "apps/fm/cla.pdf", "apps/fm/build.mk"],
        language="C",
    )
    body = _components_page([comp], {}, None, _Counter(), 1000).body
    assert "fm_app.c" in body
    assert "LICENSE" not in body
    assert "cla.pdf" not in body
    assert "build.mk" not in body
    assert "3 more file(s) not shown" in body
