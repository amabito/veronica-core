"""Shield exception types."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.shield.types import Decision, ToolCallContext


class ShieldBlockedError(Exception):
    """Raised when the shield pipeline returns a non-ALLOW decision."""

    def __init__(
        self,
        decision: Decision,
        reason: str,
        ctx: ToolCallContext | None = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.ctx = ctx
        super().__init__(f"Shield blocked: {decision.value} -- {reason}")
