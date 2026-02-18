"""SafeMode hook for VERONICA Execution Shield.

When enabled, acts as an emergency kill-switch: blocks all tool
dispatch (pre-dispatch) and suppresses retries.  When disabled,
returns ``None`` on every check (no opinion -- defers to pipeline).
"""

from __future__ import annotations

from veronica_core.shield.types import Decision, ToolCallContext


class SafeModeHook:
    """Emergency kill-switch that halts tool calls and retries."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def before_llm_call(self, ctx: ToolCallContext) -> Decision | None:
        """Block tool dispatch when enabled and a tool_name is present."""
        if self._enabled and ctx.tool_name is not None:
            return Decision.HALT
        return None

    def on_error(
        self, ctx: ToolCallContext, err: BaseException
    ) -> Decision | None:
        """Suppress retries when enabled."""
        if self._enabled:
            return Decision.HALT
        return None
