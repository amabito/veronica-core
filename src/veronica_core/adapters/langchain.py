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

from veronica_core.adapters._shared import (
    build_adapter_container,
    extract_llm_result_cost,
    record_budget_spend,
)
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt

logger = logging.getLogger(__name__)

__all__ = ["VeronicaCallbackHandler"]


class VeronicaCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that enforces VERONICA policies.

    Hooks into the LangChain callback system to enforce budget, step count,
    and retry limits across framework-managed LLM calls.

    On each LLM invocation:
    - **Pre-call** (``on_llm_start``): policy check via AIContainer.check().
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

    def __init__(
        self,
        config: Union[GuardConfig, ExecutionConfig],
        *,
        execution_context: Any = None,
    ) -> None:
        super().__init__()
        self._container = build_adapter_container(config, execution_context)

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
        cost = extract_llm_result_cost(response)
        record_budget_spend(self._container, cost, "[VERONICA_LC]", logger)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Error hook: log error without charging budget."""
        logger.warning("[VERONICA_LC] LLM error: %s", error)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container
