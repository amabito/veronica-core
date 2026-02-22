"""LangChain + VERONICA Core — Budget & Step Enforcement via Callback Handler.

Shows how VeronicaCallbackHandler plugs into any LangChain LLM to enforce:
  - hard cost ceiling ($1.00 per session)
  - step count ceiling (5 LLM calls per session)
  - automatic VeronicaHalt when limits are exceeded

Requirements:
    pip install langchain-core
    (openai is NOT required — demo uses a stub LLM)
"""

from __future__ import annotations

import sys
from typing import Any, List
from unittest.mock import MagicMock

try:
    from langchain_core.outputs import LLMResult
    from veronica_core import GuardConfig
    from veronica_core.adapters.langchain import VeronicaCallbackHandler
    from veronica_core.inject import VeronicaHalt
except ImportError:
    print("langchain-core is not installed.")
    print("Install with: pip install langchain-core")
    print("The demo requires this package to run.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Minimal stub LLM that fires LangChain callbacks without a real API key
# ---------------------------------------------------------------------------

class _StubLLM:
    """Fires on_llm_start / on_llm_end on registered callbacks.

    Drop-in replacement for ChatOpenAI in environments without an API key.
    """

    def __init__(self, callbacks: List[Any]) -> None:
        self._callbacks = callbacks
        self._call_count = 0

    def invoke(self, prompt: str, *, cost_tokens: int = 500) -> str:
        self._call_count += 1

        # Pre-call: notify callbacks
        for cb in self._callbacks:
            cb.on_llm_start(serialized={}, prompts=[prompt])

        # Simulate LLM response
        response_text = f"[stub response {self._call_count}] to: {prompt[:40]}"

        # Build a minimal LLMResult with token usage for cost tracking
        mock_result = MagicMock(spec=LLMResult)
        mock_result.llm_output = {"token_usage": {"total_tokens": cost_tokens}}

        # Post-call: notify callbacks
        for cb in self._callbacks:
            cb.on_llm_end(mock_result)

        return response_text


# ---------------------------------------------------------------------------
# Demo 1: normal operation — calls succeed until step limit
# ---------------------------------------------------------------------------

def demo_step_limit() -> None:
    print("=" * 60)
    print("Demo 1: Step Limit (max_steps=3)")
    print("=" * 60)

    config = GuardConfig(max_cost_usd=10.0, max_steps=3)
    handler = VeronicaCallbackHandler(config)
    llm = _StubLLM(callbacks=[handler])

    prompts = [
        "Summarize the VERONICA paper",
        "List the key safety features",
        "Give a one-sentence TL;DR",
        "What are the limitations?",  # step 4 — should be blocked
    ]

    for i, prompt in enumerate(prompts, 1):
        print(f"\nCall {i}: {prompt}")
        try:
            result = llm.invoke(prompt)
            print(f"  OK: {result}")
        except VeronicaHalt as e:
            print(f"  HALTED: {e}")
            break


# ---------------------------------------------------------------------------
# Demo 2: budget enforcement — small cost cap triggers halt
# ---------------------------------------------------------------------------

def demo_budget_limit() -> None:
    print("\n" + "=" * 60)
    print("Demo 2: Budget Limit (max_cost_usd=$0.001 = 0.1 cents)")
    print("=" * 60)

    # Very tight budget: 500 tokens * $0.000002 = $0.001 per call -> hits limit on call 2
    config = GuardConfig(max_cost_usd=0.0015, max_steps=100)
    handler = VeronicaCallbackHandler(config)
    llm = _StubLLM(callbacks=[handler])

    for i in range(1, 5):
        print(f"\nCall {i} (500 tokens each, budget $0.0015 total):")
        try:
            result = llm.invoke(f"Question {i}", cost_tokens=500)
            spent = handler.container.budget.spent_usd if handler.container.budget else 0.0
            print(f"  OK: {result[:50]}")
            print(f"  Budget spent: ${spent:.4f} / $0.0015")
        except VeronicaHalt as e:
            print(f"  HALTED: {e}")
            break


# ---------------------------------------------------------------------------
# Demo 3: handler introspection
# ---------------------------------------------------------------------------

def demo_introspection() -> None:
    print("\n" + "=" * 60)
    print("Demo 3: Handler Introspection")
    print("=" * 60)

    config = GuardConfig(max_cost_usd=5.0, max_steps=10)
    handler = VeronicaCallbackHandler(config)
    llm = _StubLLM(callbacks=[handler])

    for _ in range(3):
        llm.invoke("ping", cost_tokens=200)

    container = handler.container
    print(f"\nAfter 3 calls:")
    print(f"  Steps used:   {container.step_guard.steps if container.step_guard else 'N/A'} / 10")
    spent = container.budget.spent_usd if container.budget else 0.0
    print(f"  Budget spent: ${spent:.6f} / $5.00")
    print(f"  Container:    {container!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nVERONICA Core -- LangChain Integration Showcase")
    print("VeronicaCallbackHandler plugs into any LangChain-compatible LLM.\n")

    demo_step_limit()
    demo_budget_limit()
    demo_introspection()

    print("\n" + "=" * 60)
    print("Key Takeaway:")
    print("  handler = VeronicaCallbackHandler(GuardConfig(...))")
    print("  llm = ChatOpenAI(callbacks=[handler])")
    print("  -> Budget + step limits enforced automatically on every call.")
    print("=" * 60)


if __name__ == "__main__":
    main()
