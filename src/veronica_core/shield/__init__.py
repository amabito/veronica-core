"""VERONICA Execution Shield."""
from veronica_core.shield.config import ShieldConfig
from veronica_core.shield.hooks import (
    BudgetBoundaryHook,
    EgressBoundaryHook,
    PreDispatchHook,
    RetryBoundaryHook,
)
from veronica_core.shield.noop import (
    NoopBudgetBoundaryHook,
    NoopEgressBoundaryHook,
    NoopPreDispatchHook,
    NoopRetryBoundaryHook,
)
from veronica_core.shield.types import Decision, ToolCallContext

__all__ = [
    "ShieldConfig", "Decision", "ToolCallContext",
    "PreDispatchHook", "EgressBoundaryHook", "RetryBoundaryHook", "BudgetBoundaryHook",
    "NoopPreDispatchHook", "NoopEgressBoundaryHook", "NoopRetryBoundaryHook", "NoopBudgetBoundaryHook",
]
