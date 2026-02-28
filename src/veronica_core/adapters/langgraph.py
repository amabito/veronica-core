"""veronica_core.adapters.langgraph — LangGraph node wrapper and callback handler.

Integrates VERONICA policy enforcement into LangGraph pipelines via two
complementary approaches:

1. VeronicaLangGraphCallback — mirrors the VeronicaCallbackHandler interface
   for use in LangGraph's callback system (LangGraph uses the same LangChain
   callback interface internally).

2. veronica_node_wrapper() — a decorator that wraps any LangGraph node
   function with VERONICA policy enforcement (pre-node check + post-node
   step/cost recording).

This module raises ImportError on import if langgraph is not installed.

Public API:
    VeronicaLangGraphCallback — callback handler for LangGraph pipelines.
    veronica_node_wrapper     — node decorator for direct LangGraph integration.

Usage::

    from langgraph.graph import StateGraph
    from veronica_core.adapters.langgraph import veronica_node_wrapper
    from veronica_core import GuardConfig

    config = GuardConfig(max_cost_usd=1.0, max_steps=20)

    @veronica_node_wrapper(config)
    def my_node(state: dict) -> dict:
        # ... invoke LLM, update state ...
        return state

    builder = StateGraph(dict)
    builder.add_node("my_node", my_node)
"""
from __future__ import annotations

try:
    import langgraph  # noqa: F401 — presence check only
except ImportError as _exc:
    raise ImportError(
        "veronica_core.adapters.langgraph requires langgraph. "
        "Install with: pip install langgraph"
    ) from _exc

import functools
import logging
from typing import Any, Callable, TypeVar, Union

from veronica_core.adapters._shared import build_container, cost_from_total_tokens
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

__all__ = ["VeronicaLangGraphCallback", "veronica_node_wrapper"]

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_token_cost(result: Any) -> float:
    """Best-effort extraction of USD cost from a LangGraph node return value.

    LangGraph nodes return state dicts; token usage may be embedded under
    various keys depending on how the node was implemented. Returns 0.0 when
    no usage information is found.
    """
    if not isinstance(result, dict):
        return 0.0
    try:
        model = result.get("model_name") or result.get("model") or ""
        usage = result.get("token_usage") or result.get("usage")
        if usage is None:
            llm_output = result.get("llm_output")
            if isinstance(llm_output, dict):
                usage = llm_output.get("token_usage") or llm_output.get("usage")
        if not isinstance(usage, dict):
            return 0.0
        total = usage.get("total_tokens")
        if total is None:
            return 0.0
        return cost_from_total_tokens(int(total), model)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, RuntimeError):
        return 0.0


# ---------------------------------------------------------------------------
# VeronicaLangGraphCallback
# ---------------------------------------------------------------------------


class VeronicaLangGraphCallback:
    """LangGraph-compatible callback handler that enforces VERONICA policies.

    LangGraph uses the LangChain callback interface internally. This class
    mirrors the VeronicaCallbackHandler interface and can be passed directly
    to LangGraph's ``config`` parameter as a callback.

    On each LLM invocation within a LangGraph node:
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

        from veronica_core.adapters.langgraph import VeronicaLangGraphCallback
        from veronica_core import GuardConfig

        cb = VeronicaLangGraphCallback(GuardConfig(max_cost_usd=1.0, max_steps=20))
        # Pass to LangGraph via RunnableConfig callbacks=[cb]
    """

    def __init__(self, config: Union[GuardConfig, ExecutionConfig]) -> None:
        self._container = build_container(config)

    # ------------------------------------------------------------------
    # LangChain-compatible callback hooks (used by LangGraph internally)
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Pre-call hook: enforce policies before the LLM is invoked.

        Raises:
            VeronicaHalt: If any active policy (budget / step / retry) denies.
        """
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Post-call hook: increment step counter and record token cost."""
        if self._container.step_guard is not None:
            self._container.step_guard.step()

        if self._container.budget is not None:
            cost = _extract_llm_result_cost(response)
            within = self._container.budget.spend(cost)
            if not within:
                logger.warning(
                    "[VERONICA_LG] LLM call pushed budget over limit "
                    "(spent $%.4f / $%.4f)",
                    self._container.budget.spent_usd,
                    self._container.budget.limit_usd,
                )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Error hook: log error without charging budget."""
        logger.warning("[VERONICA_LG] LLM error: %s", error)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container


def _extract_llm_result_cost(response: Any) -> float:
    """Extract USD cost from a LangChain LLMResult (used by callback handler).

    Mirrors the logic in adapters/langchain.py _estimate_cost() so that
    VeronicaLangGraphCallback works with the same LLMResult objects when
    LangGraph invokes the callbacks.
    """
    try:
        if response is None:
            return 0.0
        llm_output = getattr(response, "llm_output", None)
        if llm_output is None:
            return 0.0
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if not usage:
            return 0.0

        model = llm_output.get("model_name") or llm_output.get("model") or ""

        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

        if prompt_tokens is not None and completion_tokens is not None:
            return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        total_raw = usage.get("total_tokens")
        if total_raw is None:
            return 0.0
        return cost_from_total_tokens(int(total_raw), model)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, RuntimeError):
        return 0.0


# ---------------------------------------------------------------------------
# veronica_node_wrapper
# ---------------------------------------------------------------------------


def veronica_node_wrapper(
    config: Union[GuardConfig, ExecutionConfig],
    *,
    container: AIContainer | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps a LangGraph node function with VERONICA policy enforcement.

    Wraps any LangGraph node callable so that:
    - **Pre-node**: AIContainer.check() is called; raises VeronicaHalt if denied.
    - **Post-node**: step counter incremented; cost recorded if token usage
      is present in the return value state dict.

    Args:
        config: GuardConfig or ExecutionConfig specifying policy limits.
            Ignored when ``container`` is provided.
        container: Optional pre-built AIContainer. If provided, ``config``
            is ignored. Useful for sharing a container across multiple nodes.

    Returns:
        A decorator that wraps the node function.

    Raises:
        VeronicaHalt: Before the node executes if any policy denies.

    Example::

        from veronica_core.adapters.langgraph import veronica_node_wrapper
        from veronica_core import GuardConfig

        config = GuardConfig(max_cost_usd=2.0, max_steps=10)

        @veronica_node_wrapper(config)
        def call_model(state: dict) -> dict:
            # ... LLM call ...
            return {"messages": [...], "token_usage": {"total_tokens": 350}}
    """
    _container = container if container is not None else build_container(config)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Pre-node: policy check
            decision = _container.check(cost_usd=0.0)
            if not decision.allowed:
                raise VeronicaHalt(decision.reason, decision)

            # Execute the node
            result = fn(*args, **kwargs)

            # Post-node: step increment
            if _container.step_guard is not None:
                _container.step_guard.step()

            # Post-node: cost recording from state dict
            if _container.budget is not None:
                cost = _extract_token_cost(result)
                if cost > 0.0:
                    within = _container.budget.spend(cost)
                    if not within:
                        logger.warning(
                            "[VERONICA_LG] Node '%s' pushed budget over limit "
                            "(spent $%.4f / $%.4f)",
                            fn.__name__,
                            _container.budget.spent_usd,
                            _container.budget.limit_usd,
                        )

            return result

        # Expose container for testing and introspection
        wrapper.container = _container  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
