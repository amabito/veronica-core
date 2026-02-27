"""veronica_core.adapters.llamaindex — LlamaIndex callback handler.

Integrates VERONICA policy enforcement into LlamaIndex pipelines via the
standard BaseCallbackHandler interface. Requires llama-index-core or
llama_index (legacy).

This module uses a try/except ImportError pattern so llama_index is an
optional dependency. An ImportError is raised at class instantiation time
(not at module import time) if neither package is installed. This allows
the rest of veronica_core to import cleanly in environments without LlamaIndex.

Public API:
    VeronicaLlamaIndexHandler — BaseCallbackHandler subclass enforcing
        budget, step count, circuit breaker, and retry limits on every
        LLM call event.

Usage::

    from llama_index.core import Settings
    from veronica_core.adapters.llamaindex import VeronicaLlamaIndexHandler
    from veronica_core import GuardConfig

    handler = VeronicaLlamaIndexHandler(GuardConfig(max_cost_usd=1.0, max_steps=20))
    Settings.callback_manager.add_handler(handler)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from veronica_core.agent_guard import AgentStepGuard
from veronica_core.budget import BudgetEnforcer
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.container import AIContainer
from veronica_core.containment import ExecutionConfig
from veronica_core.inject import GuardConfig, VeronicaHalt
from veronica_core.pricing import estimate_cost_usd
from veronica_core.retry import RetryContainer
from veronica_core.runtime_policy import PolicyContext

logger = logging.getLogger(__name__)

__all__ = ["VeronicaLlamaIndexHandler"]

# ---------------------------------------------------------------------------
# Optional llama_index import — deferred to instantiation time
# ---------------------------------------------------------------------------

_LLAMA_INDEX_AVAILABLE: bool = False
_BaseCallbackHandler: Any = object  # placeholder base class

try:
    # llama-index >= 0.10 (llama-index-core)
    from llama_index.core.callbacks import BaseCallbackHandler as _LI_CBH  # type: ignore[import]
    from llama_index.core.callbacks.schema import CBEventType  # type: ignore[import]

    _BaseCallbackHandler = _LI_CBH
    _LLAMA_INDEX_AVAILABLE = True
except ImportError:
    try:
        # Legacy llama_index < 0.10
        from llama_index.callbacks import BaseCallbackHandler as _LI_CBH  # type: ignore[import]
        from llama_index.callbacks.schema import CBEventType  # type: ignore[import]

        _BaseCallbackHandler = _LI_CBH
        _LLAMA_INDEX_AVAILABLE = True
    except ImportError:
        # Neither package installed — define a minimal stub so that
        # VeronicaLlamaIndexHandler can be defined at module level without error.
        # Attempting to instantiate it will raise ImportError.
        CBEventType = None  # type: ignore[assignment]


class VeronicaLlamaIndexHandler(_BaseCallbackHandler):  # type: ignore[valid-type]
    """LlamaIndex callback handler that enforces VERONICA policies.

    Hooks into the LlamaIndex callback system to enforce budget, step count,
    circuit breaker, and retry limits across framework-managed LLM calls.

    On each LLM invocation:
    - **Pre-call** (``on_event_start`` with ``CBEventType.LLM``): policy check
      via AIcontainer.check(). Raises VeronicaHalt if any policy denies.
    - **Post-call** (``on_event_end`` with ``CBEventType.LLM``): increments step
      counter; records token cost into BudgetEnforcer if usage is available.

    Args:
        config: GuardConfig or ExecutionConfig specifying limits.
            Both expose max_cost_usd, max_steps, max_retries_total.
        circuit_breaker: Optional CircuitBreaker for per-entity failure tracking.
            If provided, LLM errors open the circuit; successes close it.
        entity_id: Entity identifier used with circuit_breaker (default: "llm").

    Raises:
        ImportError: If llama-index-core (or legacy llama_index) is not installed.
        VeronicaHalt: When a policy denies execution on ``on_event_start``.

    Example::

        from llama_index.core import Settings
        from veronica_core.adapters.llamaindex import VeronicaLlamaIndexHandler
        from veronica_core import GuardConfig

        handler = VeronicaLlamaIndexHandler(GuardConfig(max_cost_usd=1.0, max_steps=20))
        Settings.callback_manager.add_handler(handler)
    """

    def __init__(
        self,
        config: Union[GuardConfig, ExecutionConfig],
        circuit_breaker: Optional[CircuitBreaker] = None,
        entity_id: str = "llm",
    ) -> None:
        if not _LLAMA_INDEX_AVAILABLE:
            raise ImportError(
                "veronica_core.adapters.llamaindex requires llama-index-core or llama_index. "
                "Install with: pip install llama-index-core"
            )

        # LlamaIndex BaseCallbackHandler requires event_starts_to_ignore and
        # event_ends_to_ignore lists.
        super().__init__(
            event_starts_to_ignore=[],
            event_ends_to_ignore=[],
        )

        self._container = AIContainer(
            budget=BudgetEnforcer(limit_usd=config.max_cost_usd),
            retry=RetryContainer(max_retries=config.max_retries_total),
            step_guard=AgentStepGuard(max_steps=config.max_steps),
        )
        self._circuit_breaker = circuit_breaker
        self._entity_id = entity_id

    # ------------------------------------------------------------------
    # LlamaIndex callback hooks
    # ------------------------------------------------------------------

    def on_event_start(
        self,
        event_type: Any,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Pre-call hook: enforce policies before the LLM is invoked.

        Only acts on ``CBEventType.LLM`` events; all others pass through.

        Args:
            event_type: LlamaIndex CBEventType enum value.
            payload: Event payload dict (may contain model, messages, etc.).
            event_id: Unique event identifier (auto-generated if empty).
            parent_id: Parent event identifier.
            **kwargs: Additional keyword arguments (ignored).

        Returns:
            event_id string (required by LlamaIndex callback protocol).

        Raises:
            VeronicaHalt: If any active policy (budget / step / retry) denies.
        """
        if CBEventType is not None and event_type == CBEventType.LLM:
            # Check circuit breaker before policy container
            if self._circuit_breaker is not None:
                cb_ctx = PolicyContext(entity_id=self._entity_id)
                cb_decision = self._circuit_breaker.check(cb_ctx)
                if not cb_decision.allowed:
                    raise VeronicaHalt(cb_decision.reason, cb_decision)

            decision = self._container.check(cost_usd=0.0)
            if not decision.allowed:
                raise VeronicaHalt(decision.reason, decision)

        return event_id or str(uuid4())

    def on_event_end(
        self,
        event_type: Any,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Post-call hook: record token cost and increment step counter.

        Only acts on ``CBEventType.LLM`` events; all others pass through.

        Args:
            event_type: LlamaIndex CBEventType enum value.
            payload: Event payload dict (may contain usage info).
            event_id: Unique event identifier.
            **kwargs: Additional keyword arguments (ignored).
        """
        if CBEventType is None or event_type != CBEventType.LLM:
            return

        # Increment step counter
        if self._container.step_guard is not None:
            self._container.step_guard.step()

        # Record token cost against budget
        if self._container.budget is not None:
            cost = _extract_cost_from_payload(payload)
            if cost > 0.0:
                within = self._container.budget.spend(cost)
                if not within:
                    logger.warning(
                        "[VERONICA_LI] LLM call pushed budget over limit "
                        "(spent $%.4f / $%.4f)",
                        self._container.budget.spent_usd,
                        self._container.budget.limit_usd,
                    )

        # Record circuit breaker success
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        """Called when a trace starts. No-op for VERONICA handler."""

    def end_trace(
        self,
        trace_id: Optional[str] = None,
        trace_map: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        """Called when a trace ends. No-op for VERONICA handler."""

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Error hook: log error and record circuit breaker failure."""
        logger.warning("[VERONICA_LI] LLM error: %s", error)
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_failure(error=error)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def container(self) -> AIContainer:
        """The underlying AIContainer (for testing and introspection)."""
        return self._container

    @property
    def circuit_breaker(self) -> Optional[CircuitBreaker]:
        """The optional CircuitBreaker (for testing and introspection)."""
        return self._circuit_breaker


def _extract_cost_from_payload(payload: Optional[Dict[str, Any]]) -> float:
    """Extract a USD cost estimate from a LlamaIndex LLM event payload.

    LlamaIndex may include usage information in several locations within the
    payload dict. Tries each known location in order and returns 0.0 if none
    can be parsed.

    Args:
        payload: The LlamaIndex event payload dict from on_event_end.

    Returns:
        Estimated cost in USD (non-negative float).
    """
    if not payload:
        return 0.0

    try:
        # LlamaIndex >= 0.10: payload may contain a response object
        response = payload.get("response")
        model = ""

        # Extract model name from various payload locations
        raw_output = payload.get("raw_output") or {}
        if isinstance(raw_output, dict):
            model = raw_output.get("model") or raw_output.get("model_name") or ""

        # Try structured usage from LlamaIndex response
        if response is not None:
            raw = getattr(response, "raw", None)
            if isinstance(raw, dict):
                usage = raw.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                completion_tokens = (
                    usage.get("completion_tokens") or usage.get("output_tokens")
                )
                if prompt_tokens is not None and completion_tokens is not None:
                    model_from_raw = raw.get("model") or raw.get("model_name") or model
                    return estimate_cost_usd(
                        model_from_raw, int(prompt_tokens), int(completion_tokens)
                    )

        # Fallback: look for token counts directly in payload
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
            completion_tokens = (
                usage.get("completion_tokens") or usage.get("output_tokens")
            )
            if prompt_tokens is not None and completion_tokens is not None:
                return estimate_cost_usd(model, int(prompt_tokens), int(completion_tokens))

        return 0.0
    except Exception:
        return 0.0
