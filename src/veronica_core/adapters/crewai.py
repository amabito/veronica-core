"""veronica_core.adapters.crewai — CrewAI event listener adapter.

Integrates VERONICA policy enforcement into CrewAI pipelines via the
standard BaseEventListener / crewai_event_bus interface. Requires crewai.

This module raises ImportError on import if crewai is not installed.

Public API:
    VeronicaCrewAIListener — BaseEventListener subclass that:
        - Monitors LLM call events to increment step counters and record costs.
        - Exposes ``check_or_raise()`` for explicit pre-call policy enforcement
          in CrewAI ``step_callback`` / ``task_callback`` integrations.

    Note: CrewAI's event bus swallows handler exceptions, so VeronicaHalt
    cannot be raised directly from LLMCallStartedEvent handlers. Instead, use
    ``step_callback=listener.check_or_raise`` on your Crew instance, or call
    ``listener.check_or_raise()`` explicitly before LLM invocations.

Usage (step_callback)::

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
from typing import Any, Union

from veronica_core.adapters._shared import build_container, cost_from_total_tokens
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
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

    Args:
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.

    Raises:
        VeronicaHalt: When ``check_or_raise()`` is called and a policy denies.

    Example::

        from veronica_core.adapters.crewai import VeronicaCrewAIListener
        from veronica_core import GuardConfig

        listener = VeronicaCrewAIListener(GuardConfig(max_cost_usd=1.0, max_steps=20))
        crew = Crew(agents=[...], tasks=[...], step_callback=listener.check_or_raise)
    """

    def __init__(self, config: Union[GuardConfig, ExecutionConfig]) -> None:
        # _container must be set BEFORE super().__init__() because
        # BaseEventListener.__init__() calls setup_listeners(), which
        # registers closures that capture self._container.
        self._container = build_container(config)
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

            if self._container.budget is not None:
                cost = _estimate_cost(event)
                within = self._container.budget.spend(cost)
                if not within:
                    logger.warning(
                        "[VERONICA_CREW] LLM call pushed budget over limit "
                        "(spent $%.4f / $%.4f)",
                        self._container.budget.spent_usd,
                        self._container.budget.limit_usd,
                    )

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
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

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
        prompt_tokens = _get_field(usage, "prompt_tokens", "input_tokens")
        completion_tokens = _get_field(usage, "completion_tokens", "output_tokens")

        if prompt_tokens is not None and completion_tokens is not None:
            return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        total_raw = _get_field(usage, "total_tokens")
        if total_raw is None:
            return 0.0

        return cost_from_total_tokens(int(total_raw), model)
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError, RuntimeError):
        return 0.0


def _get_field(obj: Any, *keys: str) -> Any:
    """Extract the first non-None value from obj by trying each key.

    Supports both attribute access (objects) and dict access (mappings).
    """
    for key in keys:
        val = getattr(obj, key, None) if not isinstance(obj, dict) else obj.get(key)
        if val is not None:
            return val
    return None
