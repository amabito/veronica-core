"""No-op hook implementations for VERONICA Execution Shield.

Each class satisfies the corresponding Protocol and always returns
``None`` (no opinion), making them safe defaults.
"""

from __future__ import annotations

from veronica_core.shield.types import Decision, ToolCallContext


class NoopPreDispatchHook:
    """Pre-dispatch hook that always defers."""

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        return None


class NoopEgressBoundaryHook:
    """Egress boundary hook that always defers."""

    def before_egress(
        self, ctx: ToolCallContext, url: str, method: str
    ) -> Decision | None:
        return None


class NoopRetryBoundaryHook:
    """Retry boundary hook that always defers."""

    def on_error(
        self, ctx: ToolCallContext, err: BaseException
    ) -> Decision | None:
        return None


class NoopBudgetBoundaryHook:
    """Budget boundary hook that always defers."""

    def before_charge(
        self, ctx: ToolCallContext, cost_usd: float
    ) -> Decision | None:
        return None
