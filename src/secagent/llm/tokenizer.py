"""Gemma-aware token counting for context budgeting.

Accurate budgeting matters most for local models with small windows (e.g. Gemma 2's
8k). When a real Gemma tokenizer file is available we use it; otherwise we fall back
to a deterministic heuristic that slightly *over*-estimates, so we never silently
overflow the model's window.

The precise tokenizer is opt-in and offline-friendly: set ``SECAGENT_TOKENIZER_FILE``
to a local ``tokenizer.json`` (no network/HF download required).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

# Subword-ish splitter for the heuristic: words, numbers, and individual symbols.
_PIECE_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class TokenCounter:
    """Counts tokens, preferring a real tokenizer when one is configured."""

    def __init__(self, tokenizer_file: str | None = None) -> None:
        self._backend = None
        path = tokenizer_file or os.environ.get("SECAGENT_TOKENIZER_FILE")
        if path and os.path.exists(path):
            self._backend = _load_tokenizer(path)

    @property
    def exact(self) -> bool:
        return self._backend is not None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._backend is not None:
            return len(self._backend.encode(text).ids)
        return _heuristic_count(text)

    def count_messages(self, messages: list[dict]) -> int:
        """Approximate token count for a chat message list (incl. role overhead)."""
        total = 0
        for m in messages:
            # ~4 tokens of structural overhead per message (role markers, separators).
            total += 4
            content = m.get("content") or ""
            if isinstance(content, str):
                total += self.count(content)
            total += self.count(str(m.get("tool_calls", "")))
        return total + 2


def _heuristic_count(text: str) -> int:
    """Deterministic, slightly conservative token estimate.

    Uses the larger of a piece-count estimate and a bytes/4 estimate, then adds a
    10% safety margin. Code (dense punctuation) and prose both round up rather than
    down, which is the safe direction for budgeting.
    """
    pieces = len(_PIECE_RE.findall(text))
    bytes_est = len(text.encode("utf-8")) / 4.0
    base = max(pieces, bytes_est)
    return int(base * 1.1) + 1


@lru_cache(maxsize=4)
def _load_tokenizer(path: str):
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None
    try:
        return Tokenizer.from_file(path)
    except Exception:
        return None


_default = TokenCounter()


def count_tokens(text: str) -> int:
    """Module-level convenience using the default counter."""
    return _default.count(text)
