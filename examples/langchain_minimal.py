"""LangChain minimal — Budget enforcement via VeronicaCallbackHandler.

Attach VeronicaCallbackHandler to any LangChain LLM to enforce a hard
cost ceiling and step count limit on every invocation. No real API key
required — this example uses a stub that fires the callback hooks directly.

Install:
    pip install veronica-core langchain-core
"""

from __future__ import annotations

from typing import Any, List

from langchain_core.outputs import LLMResult

from veronica_core import GuardConfig
from veronica_core.adapters.langchain import VeronicaCallbackHandler
from veronica_core.inject import VeronicaHalt


class _StubLLM:
    """Fires LangChain callbacks without a real API call.

    Replace with ChatOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "sk-..."))
    in production.
    """

    def __init__(self, callbacks: List[Any]) -> None:
        self._callbacks = callbacks

    def invoke(self, prompt: str, *, tokens: int = 500) -> str:
        # Pre-call: let VERONICA check budget and step count
        for cb in self._callbacks:
            cb.on_llm_start(serialized={}, prompts=[prompt])

        # Simulate LLM response with real LLMResult so cost is extracted
        result = LLMResult(
            generations=[],
            llm_output={"token_usage": {"total_tokens": tokens}},
        )

        # Post-call: record token cost and increment step counter
        for cb in self._callbacks:
            cb.on_llm_end(result)

        return f"[stub] response to: {prompt[:40]}"


def main() -> None:
    # 1. Configure limits: $0.10 total, 3 LLM steps
    config = GuardConfig(max_cost_usd=0.10, max_steps=3)
    handler = VeronicaCallbackHandler(config)

    # 2. Attach handler to your LLM
    llm = _StubLLM(callbacks=[handler])

    # 3. Call as normal — handler enforces limits transparently
    prompts = ["Summarize VERONICA", "List key features", "Give a TL;DR", "What are limits?"]
    for i, prompt in enumerate(prompts, 1):
        print(f"Call {i}: {prompt}")
        try:
            reply = llm.invoke(prompt, tokens=500)
            budget = handler.container.budget
            steps = handler.container.step_guard
            spent = budget.spent_usd if budget else 0.0
            used = steps.current_step if steps else 0
            print(f"  OK: {reply}")
            print(f"  budget=${spent:.4f}/{config.max_cost_usd}  steps={used}/{config.max_steps}")
        except VeronicaHalt as exc:
            print(f"  HALTED: {exc}")
            break


if __name__ == "__main__":
    main()
