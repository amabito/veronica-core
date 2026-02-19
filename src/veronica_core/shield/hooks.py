"""Hook protocols for VERONICA Execution Shield.

Each protocol represents a boundary where the shield can intercept
and make a Decision.  Returning ``None`` means "no opinion" --
the pipeline (when wired) will treat it as ALLOW.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from veronica_core.shield.types import Decision, ToolCallContext


@runtime_checkable
class PreDispatchHook(Protocol):
    """Evaluated before every LLM / tool call."""

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None: ...


@runtime_checkable
class EgressBoundaryHook(Protocol):
    """Evaluated before an outbound HTTP request."""

    def before_egress(
        self, ctx: ToolCallContext, url: str, method: str
    ) -> Decision | None: ...


@runtime_checkable
class RetryBoundaryHook(Protocol):
    """Evaluated when a tool call raises an exception."""

    def on_error(
        self, ctx: ToolCallContext, err: BaseException
    ) -> Decision | None: ...


@runtime_checkable
class BudgetBoundaryHook(Protocol):
    """Evaluated before recording a cost charge."""

    def before_charge(
        self, ctx: ToolCallContext, cost_usd: float
    ) -> Decision | None: ...


@runtime_checkable
class ToolDispatchHook(Protocol):
    """Evaluated before every tool call (non-LLM dispatch)."""

    def before_tool_call(self, ctx: ToolCallContext) -> Decision | None: ...
