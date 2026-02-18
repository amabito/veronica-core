"""Shield pipeline -- evaluates hooks and resolves Decisions.

The pipeline calls each hook if present.  A hook returning ``None``
is treated as ALLOW.  No side effects, no feature logic.
"""

from __future__ import annotations

from veronica_core.shield.hooks import (
    BudgetBoundaryHook,
    EgressBoundaryHook,
    PreDispatchHook,
    RetryBoundaryHook,
)
from veronica_core.shield.types import Decision, ToolCallContext


class ShieldPipeline:
    """Evaluates registered hooks and returns a Decision."""

    def __init__(
        self,
        pre_dispatch: PreDispatchHook | None = None,
        egress: EgressBoundaryHook | None = None,
        retry: RetryBoundaryHook | None = None,
        budget: BudgetBoundaryHook | None = None,
    ) -> None:
        self._pre_dispatch = pre_dispatch
        self._egress = egress
        self._retry = retry
        self._budget = budget

    def before_llm_call(self, ctx: ToolCallContext) -> Decision:
        """Evaluate pre-dispatch hook."""
        if self._pre_dispatch is not None:
            result = self._pre_dispatch.before_llm_call(ctx)
            if result is not None:
                return result
        return Decision.ALLOW

    def before_egress(self, ctx: ToolCallContext, url: str, method: str) -> Decision:
        """Evaluate egress boundary hook."""
        if self._egress is not None:
            result = self._egress.before_egress(ctx, url, method)
            if result is not None:
                return result
        return Decision.ALLOW

    def on_error(self, ctx: ToolCallContext, err: BaseException) -> Decision:
        """Evaluate retry boundary hook."""
        if self._retry is not None:
            result = self._retry.on_error(ctx, err)
            if result is not None:
                return result
        return Decision.ALLOW

    def before_charge(self, ctx: ToolCallContext, cost_usd: float) -> Decision:
        """Evaluate budget boundary hook."""
        if self._budget is not None:
            result = self._budget.before_charge(ctx, cost_usd)
            if result is not None:
                return result
        return Decision.ALLOW
