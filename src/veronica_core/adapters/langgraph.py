"""veronica_core.adapters.langgraph -- LangGraph node wrapper and callback handler.

Integrates VERONICA policy enforcement into LangGraph pipelines via two
complementary approaches:

1. VeronicaLangGraphCallback -- mirrors the VeronicaCallbackHandler interface
   for use in LangGraph's callback system (LangGraph uses the same LangChain
   callback interface internally).

2. veronica_node_wrapper() -- a decorator that wraps any LangGraph node
   function with VERONICA policy enforcement (pre-node check + post-node
   step/cost recording).

This module raises ImportError on import if langgraph is not installed.

Public API:
    VeronicaLangGraphCallback -- callback handler for LangGraph pipelines.
    veronica_node_wrapper     -- node decorator for direct LangGraph integration.

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
    import langgraph  # noqa: F401 -- presence check only
except ImportError as _exc:
    raise ImportError(
        "veronica_core.adapters.langgraph requires langgraph. "
        "Install with: pip install langgraph"
    ) from _exc

import functools
import logging
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar, Union

if TYPE_CHECKING:
    from veronica_core.adapter_capabilities import AdapterCapabilities

from veronica_core.adapters._shared import (
    build_adapter_container,
    build_container,
    check_and_halt,
    cost_from_total_tokens,
    emit_llm_result_tokens,
    extract_llm_result_cost,
    record_budget_spend,
)
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig

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
    except (
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
        OverflowError,
        RuntimeError,
    ):
        return 0.0


# ---------------------------------------------------------------------------
# VeronicaLangGraphCallback
# ---------------------------------------------------------------------------


class VeronicaLangGraphCallback:
    """LangGraph-compatible callback handler that enforces VERONICA policies.

    LangGraph uses the LangChain callback interface internally. This class
    mirrors the VeronicaCallbackHandler interface and can be passed directly
    to LangGraph's ``config`` parameter as a callback.

    Note (L-1): This class does not inherit from ``BaseCallbackHandler`` to avoid
    a hard dependency on langchain/langchain-core. Duck-typing works for most
    LangGraph versions. If your LangGraph version requires
    ``isinstance(cb, BaseCallbackHandler)``, install ``langchain-core`` and subclass
    this handler: ``class MyCallback(VeronicaLangGraphCallback, BaseCallbackHandler): ...``.

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

    def __init__(
        self,
        config: Union[GuardConfig, ExecutionConfig],
        *,
        execution_context: Any = None,
        metrics: Optional[Any] = None,
        agent_id: str = "langgraph",
    ) -> None:
        self._container = build_adapter_container(config, execution_context)
        self._metrics = metrics
        self._agent_id = agent_id

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
        check_and_halt(
            self._container,
            tag="[VERONICA_LG]",
            _logger=logger,
            metrics=self._metrics,
            agent_id=self._agent_id,
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Post-call hook: increment step counter and record token cost."""
        if self._container.step_guard is not None:
            self._container.step_guard.step()

        cost = extract_llm_result_cost(response)
        record_budget_spend(self._container, cost, "[VERONICA_LG]", logger)

        emit_llm_result_tokens(self._metrics, self._agent_id, response)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Error hook: log error without charging budget."""
        logger.warning("[VERONICA_LG] LLM error: %s", error)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def capabilities(self) -> "AdapterCapabilities":
        """Return the capability descriptor for this adapter."""
        from veronica_core.adapter_capabilities import AdapterCapabilities

        return AdapterCapabilities(
            framework_name="LangGraph",
            supports_cost_extraction=True,
            supports_token_extraction=True,
            supported_versions=("0.1.0", "0.2.99"),
        )

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container


# ---------------------------------------------------------------------------
# veronica_node_wrapper
# ---------------------------------------------------------------------------


def veronica_node_wrapper(
    config: Union[GuardConfig, ExecutionConfig],
    *,
    container: AIContainer | None = None,
    execution_context: Any = None,
    metrics: Optional[Any] = None,
    agent_id: str = "langgraph",
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
        metrics: Optional ContainmentMetricsProtocol. When provided,
            ``record_decision`` is emitted pre-node and ``record_tokens``
            is emitted post-node when token usage is available.
        agent_id: Identifier forwarded to metrics calls. Defaults to
            ``"langgraph"``.

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
    if container is not None:
        _container = container
    elif execution_context is not None:
        _container = build_adapter_container(config, execution_context)
    else:
        _container = build_container(config)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Pre-node: policy check
            check_and_halt(
                _container,
                tag="[VERONICA_LG]",
                _logger=logger,
                metrics=metrics,
                agent_id=agent_id,
            )

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
