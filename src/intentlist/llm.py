"""Thin Claude wrapper: forced tool use gives schema-bound JSON output."""
from __future__ import annotations

from typing import Any

import anthropic

from .settings import Settings


class LLMError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, settings: Settings, client: Any | None = None):
        if client is None:
            if not settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4,
                                         timeout=settings.request_timeout_s * 3)
        self.client = client
        self.model = settings.anthropic_model
        self.in_rate = settings.cost_llm_input_mtok / 1_000_000
        self.out_rate = settings.cost_llm_output_mtok / 1_000_000

    def call_tool(self, prompt: str, tool: dict, system: str | None = None, max_tokens: int = 4000) -> tuple[dict, dict]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self.client.messages.create(**kwargs)
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            raise LLMError("model did not return a tool call")
        in_tok = getattr(resp.usage, "input_tokens", 0) or 0
        out_tok = getattr(resp.usage, "output_tokens", 0) or 0
        usage = {"input_tokens": in_tok, "output_tokens": out_tok,
                 "cost_usd": in_tok * self.in_rate + out_tok * self.out_rate, "model": self.model}
        return dict(block.input), usage
