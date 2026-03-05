"""veronica_core.adapters.crewai — CrewAI event listener adapter.

Integrates VERONICA policy enforcement into CrewAI pipelines via the
standard BaseEventListener / crewai_event_bus interface. Requires crewai.

This module raises ImportError on import if crewai is not installed.

Public API:
    VeronicaCrewAIListener — BaseEventListener subclass that:
        - Monitors LLM call events to increment step counters and record costs.
        - Exposes ``check_or_raise()`` for explicit pre-call policy enforcement
          in CrewAI ``step_callback`` / ``task_callback`` integrations.
        - Accepts an optional ``execution_context`` kwarg to enforce chain-level
          budget and step limits shared across multiple adapters or agents.

    Note: CrewAI's event bus swallows handler exceptions, so VeronicaHalt
    cannot be raised directly from LLMCallStartedEvent handlers. Instead, use
    ``step_callback=listener.check_or_raise`` on your Crew instance, or call
    ``listener.check_or_raise()`` explicitly before LLM invocations.

    When ``execution_context`` is provided, the adapter enforces chain-level
    limits. The event bus handler (``on_llm_call_started``) still cannot raise
    due to CrewAI swallowing handler exceptions — use ``check_or_raise()`` as
    the ``step_callback`` for HALT enforcement.

Usage (step_callback with ExecutionContext)::

    from crewai import Crew
    from veronica_core.adapters.crewai import VeronicaCrewAIListener
    from veronica_core import GuardConfig
    from veronica_core.containment import ExecutionContext, ExecutionConfig

    config = ExecutionConfig(max_cost_usd=1.0, max_steps=20)
    ctx = ExecutionContext(config=config)
    listener = VeronicaCrewAIListener(config, execution_context=ctx)
    crew = Crew(
        agents=[...],
        tasks=[...],
        step_callback=listener.check_or_raise,
    )
    crew.kickoff()

Usage (step_callback, GuardConfig only)::

    from crewai import Crew
    from veronica_core.adapters.crewai import VeronicaCrewAIListener
    from veronica_core import GuardConfig

    listener = VeronicaCrewAIListener(GuardConfig(max_cost_usd=1.0, max_steps=20))
    crew = Crew(
        agents=[...],
        tasks=[...],
        step_callback=listener.check_or_raise,
    )
    crew.kickoff()

Usage (event bus only, for monitoring)::

    from veronica_core.adapters.crewai import VeronicaCrewAIListener
    from veronica_core import GuardConfig

    listener = VeronicaCrewAIListener(GuardConfig(max_cost_usd=1.0, max_steps=20))
    # Events are automatically monitored; inspect listener.container for state.
"""

from __future__ import annotations

try:
    from crewai.events import BaseEventListener  # type: ignore[import]
    from crewai.events.types.llm_events import (  # type: ignore[import]
        LLMCallCompletedEvent,
        LLMCallFailedEvent,
        LLMCallStartedEvent,
    )
except ImportError as _exc:
    raise ImportError(
        "veronica_core.adapters.crewai requires crewai. "
        "Install with: pip install crewai"
    ) from _exc

import logging
from typing import Any, Optional, Union

from veronica_core.adapters._shared import (
    build_adapter_container,
    check_and_halt,
    cost_from_total_tokens,
    get_field,
    record_budget_spend,
    safe_emit,
)
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig
from veronica_core.pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

__all__ = ["VeronicaCrewAIListener"]


class VeronicaCrewAIListener(BaseEventListener):
    """CrewAI event listener that enforces VERONICA policies.

    Hooks into the CrewAI event bus to track LLM calls and enforce budget,
    step count, and retry limits. Because the CrewAI event bus catches
    exceptions raised inside handlers, explicit policy enforcement (raising
    VeronicaHalt) must be done via ``check_or_raise()``.

    On each LLM invocation (via event bus):
    - **Pre-call** (``LLMCallStartedEvent``): policy check logged; use
      ``check_or_raise()`` as a ``step_callback`` to raise VeronicaHalt.
    - **Post-call** (``LLMCallCompletedEvent``): increments step counter; records
      token cost from the response into BudgetEnforcer.

    When ``execution_context`` is provided, chain-level limits from that context
    are enforced in addition to (or instead of) the config-level limits.
    ``check_or_raise()`` will block once the chain-level budget or step limit
    is exceeded.

    Args:
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.
        execution_context: Optional chain-level ExecutionContext. When provided,
            the adapter wraps the context to enforce shared limits across
            multiple agents or adapters within the same run.

    Raises:
        VeronicaHalt: When ``check_or_raise()`` is called and a policy denies.

    Example::

        from veronica_core.adapters.crewai import VeronicaCrewAIListener
        from veronica_core import GuardConfig

        listener = VeronicaCrewAIListener(GuardConfig(max_cost_usd=1.0, max_steps=20))
        crew = Crew(agents=[...], tasks=[...], step_callback=listener.check_or_raise)
    """

    def __init__(
        self,
        config: Union[GuardConfig, ExecutionConfig],
        *,
        execution_context: Optional[Any] = None,
        metrics: Optional[Any] = None,
        agent_id: str = "crewai",
    ) -> None:
        # _container must be set BEFORE super().__init__() because
        # BaseEventListener.__init__() calls setup_listeners(), which
        # registers closures that capture self._container.
        self._container = build_adapter_container(config, execution_context)
        self._metrics = metrics
        self._agent_id = agent_id
        super().__init__()

    # ------------------------------------------------------------------
    # BaseEventListener implementation
    # ------------------------------------------------------------------

    def setup_listeners(self, bus: Any) -> None:
        """Register VERONICA monitoring hooks on the CrewAI event bus.

        LLMCallStartedEvent: Logs a warning when policies would deny.
            Cannot raise VeronicaHalt here because the CrewAI event bus
            swallows handler exceptions. Use ``check_or_raise()`` instead.

        LLMCallCompletedEvent: Increments step counter and records token cost.

        LLMCallFailedEvent: Logs the error without charging budget.

        Args:
            bus: The CrewAI event bus instance.
        """

        @bus.on(LLMCallStartedEvent)
        def on_llm_call_started(source: Any, event: LLMCallStartedEvent) -> None:
            """Pre-call hook: log policy state before the LLM is invoked."""
            decision = self._container.check(cost_usd=0.0)
            if not decision.allowed:
                logger.warning(
                    "[VERONICA_CREW] Policy denied LLM call: %s. "
                    "Call check_or_raise() in step_callback to enforce.",
                    decision.reason,
                )

        @bus.on(LLMCallCompletedEvent)
        def on_llm_call_completed(source: Any, event: LLMCallCompletedEvent) -> None:
            """Post-call hook: increment step counter and record token cost."""
            if self._container.step_guard is not None:
                self._container.step_guard.step()

            cost = _estimate_cost(event)
            record_budget_spend(self._container, cost, "[VERONICA_CREW]", logger)

            # Emit token metrics when a backend is configured
            _emit_event_tokens(self._metrics, self._agent_id, event)

        @bus.on(LLMCallFailedEvent)
        def on_llm_call_failed(source: Any, event: LLMCallFailedEvent) -> None:
            """Error hook: log error without charging budget."""
            logger.warning("[VERONICA_CREW] LLM call failed: %s", event.error)

    # ------------------------------------------------------------------
    # Explicit policy enforcement (for step_callback integration)
    # ------------------------------------------------------------------

    def check_or_raise(self, *args: Any, **kwargs: Any) -> None:
        """Check policies and raise VeronicaHalt if denied.

        Intended for use as a CrewAI ``step_callback`` or ``task_callback``,
        or for explicit pre-call checks. Accepts any positional/keyword args
        so it can be passed directly as a callback without signature mismatch.

        Raises:
            VeronicaHalt: If any active policy (budget / step / retry) denies.

        Example::

            crew = Crew(
                agents=[...],
                tasks=[...],
                step_callback=listener.check_or_raise,
            )
        """
        check_and_halt(
            self._container,
            tag="[VERONICA_CREW]",
            _logger=logger,
            metrics=self._metrics,
            agent_id=self._agent_id,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container


def _estimate_cost(event: LLMCallCompletedEvent) -> float:
    """Extract a USD cost estimate from a CrewAI LLMCallCompletedEvent.

    Inspects the response object for token usage fields in common formats
    (OpenAI, Anthropic, etc.). Falls back to 0.0 if usage cannot be determined.
    """
    try:
        response = event.response
        if response is None:
            return 0.0

        model = getattr(event, "model", None) or ""

        # Try to extract usage from response object attributes or dict
        usage = None
        if hasattr(response, "usage"):
            usage = response.usage
        elif hasattr(response, "usage_metadata"):
            usage = response.usage_metadata
        elif isinstance(response, dict):
            usage = response.get("usage") or response.get("usage_metadata")

        if usage is None:
            return 0.0

        # Extract token counts — support both attribute access and dict access
        prompt_tokens = get_field(usage, "prompt_tokens", "input_tokens")
        completion_tokens = get_field(usage, "completion_tokens", "output_tokens")

        if prompt_tokens is not None and completion_tokens is not None:
            return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        total_raw = get_field(usage, "total_tokens")
        if total_raw is None:
            return 0.0

        return cost_from_total_tokens(int(total_raw), model)
    except (
        AttributeError,
        TypeError,
        ValueError,
        KeyError,
        OverflowError,
        RuntimeError,
    ):
        return 0.0


def _emit_event_tokens(metrics: Any, agent_id: str, event: Any) -> None:
    """Extract token counts from a LLMCallCompletedEvent and emit via metrics."""
    try:
        response = getattr(event, "response", None)
        if response is None:
            return
        usage = None
        if hasattr(response, "usage"):
            usage = response.usage
        elif hasattr(response, "usage_metadata"):
            usage = response.usage_metadata
        elif isinstance(response, dict):
            usage = response.get("usage") or response.get("usage_metadata")
        if usage is None:
            return
        prompt = get_field(usage, "prompt_tokens", "input_tokens")
        completion = get_field(usage, "completion_tokens", "output_tokens")
        if prompt is not None and completion is not None:
            safe_emit(metrics, "record_tokens", agent_id, int(prompt), int(completion))
    except Exception:
        pass
