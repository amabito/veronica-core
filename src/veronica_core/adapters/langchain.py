"""veronica_core.adapters.langchain — LangChain callback handler.

Integrates VERONICA policy enforcement into LangChain pipelines via the
standard BaseCallbackHandler interface. Requires langchain-core or langchain.

This module raises ImportError on import if neither package is installed.

Public API:
    VeronicaCallbackHandler — BaseCallbackHandler subclass enforcing
        budget, step count, and retry limits on every LLM call.

Usage::

    from langchain_openai import ChatOpenAI
    from veronica_core.adapters.langchain import VeronicaCallbackHandler
    from veronica_core import GuardConfig

    handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=1.0, max_steps=20))
    llm = ChatOpenAI(callbacks=[handler])
    response = llm.invoke("Hello!")
"""
from __future__ import annotations

try:
    from langchain_core.callbacks import BaseCallbackHandler  # type: ignore[import]
    from langchain_core.outputs import LLMResult  # type: ignore[import]
except ImportError:
    try:
        from langchain.callbacks.base import BaseCallbackHandler  # type: ignore[import]
        from langchain.schema import LLMResult  # type: ignore[import]
    except ImportError as _exc:
        raise ImportError(
            "veronica_core.adapters.langchain requires langchain-core or langchain. "
            "Install with: pip install langchain-core"
        ) from _exc

import logging
from typing import Any, Dict, List, Union

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.budget import BudgetEnforcer
from veronica_core.container import AIcontainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.retry import RetryContainer

logger = logging.getLogger(__name__)

__all__ = ["VeronicaCallbackHandler"]


class VeronicaCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that enforces VERONICA policies.

    Hooks into the LangChain callback system to enforce budget, step count,
    and retry limits across framework-managed LLM calls.

    On each LLM invocation:
    - **Pre-call** (``on_llm_start``): policy check via AIcontainer.check().
      Raises VeronicaHalt if any policy denies.
    - **Post-call** (``on_llm_end``): increments step counter; records
      token cost from the response into BudgetEnforcer.

    Args:
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.

    Raises:
        VeronicaHalt: When a policy denies execution on ``on_llm_start``.

    Example::

        from veronica_core.adapters.langchain import VeronicaCallbackHandler
        from veronica_core import GuardConfig

        handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=1.0, max_steps=20))
        llm = ChatOpenAI(callbacks=[handler])
    """

    def __init__(self, config: Union[GuardConfig, ExecutionConfig]) -> None:
        super().__init__()
        self._container = AIcontainer(
            budget=BudgetEnforcer(limit_usd=config.max_cost_usd),
            retry=RetryContainer(max_retries=config.max_retries_total),
            step_guard=AgentStepGuard(max_steps=config.max_steps),
        )

    # ------------------------------------------------------------------
    # LangChain callback hooks
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        **kwargs: Any,
    ) -> None:
        """Pre-call hook: enforce policies before the LLM is invoked.

        Raises:
            VeronicaHalt: If any active policy (budget / step / retry) denies.
        """
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Post-call hook: increment step counter and record token cost."""
        # Increment step counter
        if self._container.step_guard is not None:
            self._container.step_guard.step()

        # Record token cost against budget
        if self._container.budget is not None:
            cost = _estimate_cost(response)
            within = self._container.budget.spend(cost)
            if not within:
                logger.warning(
                    "[VERONICA_LC] LLM call pushed budget over limit "
                    "(spent $%.4f / $%.4f)",
                    self._container.budget.spent_usd,
                    self._container.budget.limit_usd,
                )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Error hook: log error without charging budget."""
        logger.warning("[VERONICA_LC] LLM error: %s", error)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def container(self) -> AIcontainer:
        """The underlying AIcontainer (for testing and introspection)."""
        return self._container


def _estimate_cost(response: LLMResult) -> float:
    """Extract a rough USD cost estimate from a LangChain LLMResult.

    Looks for ``token_usage`` or ``usage`` in ``llm_output``.
    Returns 0.0 if the field is absent or parsing fails.
    """
    try:
        if response.llm_output is None:
            return 0.0
        usage = response.llm_output.get("token_usage") or response.llm_output.get("usage")
        if not usage:
            return 0.0
        total = usage.get("total_tokens", 0)
        return total * 0.000002  # conservative $0.002 / 1K tokens
    except Exception:
        return 0.0
