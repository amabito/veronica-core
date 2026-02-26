"""Shield pipeline -- evaluates hooks and resolves Decisions.

The pipeline calls each hook if present.  A hook returning ``None``
is treated as ALLOW.  No side effects, no feature logic.

Non-ALLOW decisions are recorded as SafetyEvent entries accessible
via ``get_events()`` / ``clear_events()``.
"""

from __future__ import annotations

import threading
from typing import Final, Optional

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.hooks import (
    BudgetBoundaryHook,
    EgressBoundaryHook,
    PreDispatchHook,
    RetryBoundaryHook,
    ToolDispatchHook,
)
from veronica_core.shield.types import Decision, ToolCallContext

# Map hook class names to structured event_type strings (read-only config).
_HOOK_EVENT_TYPES: Final[dict[str, str]] = {
    "SafeModeHook": "SAFE_MODE",
    "BudgetWindowHook": "BUDGET_WINDOW_EXCEEDED",
    "TokenBudgetHook": "TOKEN_BUDGET_EXCEEDED",
    "InputCompressionHook": "INPUT_TOO_LARGE",
    "AdaptiveBudgetHook": "ADAPTIVE_ADJUSTMENT",  # also ADAPTIVE_COOLDOWN_BLOCKED
    "TimeAwarePolicy": "TIME_POLICY_APPLIED",
    "BudgetBoundaryHook": "BUDGET_EXCEEDED",
    "EgressBoundaryHook": "EGRESS_BLOCKED",
    "RetryBoundaryHook": "RETRY_BLOCKED",
    "ToolDispatchHook": "TOOL_DISPATCH_BLOCKED",
}


def _event_type_for(hook: object) -> str:
    """Return the event_type string for a hook instance."""
    name = type(hook).__name__
    return _HOOK_EVENT_TYPES.get(name, name.upper())


class ShieldPipeline:
    """Evaluates registered hooks and returns a Decision.

    on_error policy changed in v1.0.0:
        Default changed from ALLOW to HALT (fail-closed).  Any pipeline that
        has no retry hook registered now halts on unhandled errors rather than
        allowing execution to continue.

        To restore the old behaviour explicitly::

            pipeline = ShieldPipeline(on_error_policy=Decision.ALLOW)
    """

    def __init__(
        self,
        pre_dispatch: PreDispatchHook | None = None,
        egress: EgressBoundaryHook | None = None,
        retry: RetryBoundaryHook | None = None,
        budget: BudgetBoundaryHook | None = None,
        tool_dispatch: ToolDispatchHook | None = None,
        on_error_policy: Optional[Decision] = None,
    ) -> None:
        self._pre_dispatch = pre_dispatch
        self._egress = egress
        self._retry = retry
        self._budget = budget
        self._tool_dispatch = tool_dispatch
        # None → use secure default (HALT). Callers can opt in to ALLOW explicitly.
        self._on_error_policy: Decision = (
            on_error_policy if on_error_policy is not None else Decision.HALT
        )
        self._safety_events: list[SafetyEvent] = []
        self._lock = threading.Lock()

    _MAX_SAFETY_EVENTS: int = 1000

    def _record(
        self,
        hook: object,
        decision: Decision,
        reason: str,
        request_id: str | None,
    ) -> None:
        event = SafetyEvent(
            event_type=_event_type_for(hook),
            decision=decision,
            reason=reason,
            hook=type(hook).__name__,
            request_id=request_id,
        )
        with self._lock:
            if len(self._safety_events) < self._MAX_SAFETY_EVENTS:
                self._safety_events.append(event)
        # OTel export (no-op if disabled)
        try:
            from veronica_core.otel import emit_safety_event

            emit_safety_event(event)
        except Exception:
            pass  # Never let OTel failures affect containment

    def get_events(self) -> list[SafetyEvent]:
        """Return accumulated safety events (shallow copy)."""
        with self._lock:
            return list(self._safety_events)

    def clear_events(self) -> None:
        """Clear all accumulated safety events."""
        with self._lock:
            self._safety_events.clear()

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        """Evaluate pre-dispatch hook."""
        if self._pre_dispatch is not None:
            result = self._pre_dispatch.before_llm_call(ctx)
            if result is not None:
                if result != Decision.ALLOW:
                    self._record(
                        self._pre_dispatch,
                        result,
                        f"before_llm_call returned {result.value}",
                        ctx.request_id,
                    )
                return result
        return Decision.ALLOW

    def before_egress(self, ctx: ToolCallContext, url: str, method: str) -> Decision:
        """Evaluate egress boundary hook."""
        if self._egress is not None:
            result = self._egress.before_egress(ctx, url, method)
            if result is not None:
                if result != Decision.ALLOW:
                    self._record(
                        self._egress,
                        result,
                        f"before_egress returned {result.value} for {method} {url}",
                        ctx.request_id,
                    )
                return result
        return Decision.ALLOW

    def on_error(self, ctx: ToolCallContext, err: BaseException) -> Decision:
        """Evaluate retry boundary hook.

        When a hook is registered:
          - Hook returns a Decision → use it (record if not ALLOW).
          - Hook defers (returns None) → ALLOW (explicit no-opinion).

        When no hook is registered:
          - Uses ``on_error_policy`` (default: HALT, fail-closed).
          - Pass ``on_error_policy=Decision.ALLOW`` to ShieldPipeline.__init__ to
            restore the pre-v1.0.0 behaviour explicitly.

        Default changed from ALLOW to HALT in v1.0.0 for fail-closed security.
        """
        if self._retry is not None:
            result = self._retry.on_error(ctx, err)
            if result is not None:
                if result != Decision.ALLOW:
                    self._record(
                        self._retry,
                        result,
                        f"on_error returned {result.value}: {type(err).__name__}",
                        ctx.request_id,
                    )
                return result
            # Hook deferred (returned None) — explicit no-opinion means allow.
            return Decision.ALLOW
        # No retry hook: apply the configured policy (default: HALT, fail-closed).
        return self._on_error_policy

    def before_charge(self, ctx: ToolCallContext, cost_usd: float) -> Decision:
        """Evaluate budget boundary hook."""
        if self._budget is not None:
            result = self._budget.before_charge(ctx, cost_usd)
            if result is not None:
                if result != Decision.ALLOW:
                    self._record(
                        self._budget,
                        result,
                        f"before_charge returned {result.value} for ${cost_usd:.4f}",
                        ctx.request_id,
                    )
                return result
        return Decision.ALLOW

    def before_tool_call(self, ctx: ToolCallContext) -> Decision:
        """Evaluate tool dispatch hook (tool calls only)."""
        if self._tool_dispatch is not None:
            result = self._tool_dispatch.before_tool_call(ctx)
            if result is not None:
                if result != Decision.ALLOW:
                    self._record(
                        self._tool_dispatch,
                        result,
                        f"before_tool_call returned {result.value}",
                        ctx.request_id,
                    )
                return result
        return Decision.ALLOW
