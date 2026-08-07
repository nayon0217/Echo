"""Claude client with schema-enforced structured output (policy.md §2).

Every LLM stage in ECHO returns structured data, never free text — "free-text
parsing is a correctness leak. Never regex an LLM response." We enforce this with
Anthropic tool-use: we declare a single tool whose `input_schema` is the output
schema, force the model to call it (`tool_choice`), and read the tool input.
Temperature is pinned to 0 for determinism.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from anthropic import Anthropic

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ModuleNotFoundError:
    pass

DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")


class LLMError(RuntimeError):
    """Raised when the model returns nothing usable for a stage."""


@lru_cache(maxsize=1)
def _client() -> Anthropic:
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("CLAUDE_API_KEY is not set in the environment / .env")
    return Anthropic(api_key=api_key)


def structured_call(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    tool_name: str,
    tool_description: str,
    max_tokens: int = 1024,
    model: str | None = None,
) -> dict[str, Any]:
    """Run one schema-constrained Claude call and return the tool input dict.

    `schema` is a JSON Schema object describing the required output shape.
    """
    tool = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": schema,
    }
    response = _client().messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise LLMError(f"model did not return a '{tool_name}' tool call")
