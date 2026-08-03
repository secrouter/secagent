"""Portable LLM access for secagent.

The client targets any OpenAI-compatible endpoint (llama.cpp ``llama-server`` or
vLLM). Token budgeting is Gemma-aware so the affordance engine can size context to
the deployed model's real window.
"""

from .client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMResponse,
    ToolCall,
    ToolSpec,
)
from .tokenizer import TokenCounter, count_tokens

__all__ = [
    "ChatMessage",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "ToolCall",
    "ToolSpec",
    "TokenCounter",
    "count_tokens",
]
