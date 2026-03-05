"""SafeMode hook for VERONICA Execution Shield.

When enabled, acts as an emergency kill-switch: blocks all tool
dispatch (pre-dispatch) and suppresses retries.  When disabled,
returns ``None`` on every check (no opinion -- defers to pipeline).

Note: SafeMode does NOT block HTTP egress or budget charges.
Those boundaries require separate hooks (EgressBoundaryHook,
BudgetBoundaryHook).
"""

from __future__ import annotations

from veronica_core.shield.types import Decision, ToolCallContext

__all__ = ["SafeModeHook"]


class SafeModeHook:
    """Emergency kill-switch that halts tool calls and retries."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self) -> None:
        """Disable safe mode programmatically.

        L5: Provides a proper API for disabling safe mode without accessing
        the private ``_enabled`` attribute directly.
        """
        self._enabled = False

    def enable(self) -> None:
        """Re-enable safe mode programmatically."""
        self._enabled = True

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Block tool dispatch when enabled and a tool_name is present."""
        if self._enabled and ctx.tool_name is not None:
            return Decision.HALT
        return None

    def on_error(self, ctx: ToolCallContext, err: BaseException) -> Decision | None:
        """Suppress retries when enabled."""
        if self._enabled:
            return Decision.HALT
        return None
