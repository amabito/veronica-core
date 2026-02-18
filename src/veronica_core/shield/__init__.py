"""VERONICA Execution Shield."""
from veronica_core.shield.budget_window import BudgetWindowHook
from veronica_core.shield.config import ShieldConfig
from veronica_core.shield.token_budget import TokenBudgetHook
from veronica_core.shield.errors import ShieldBlockedError
from veronica_core.shield.event import SafetyEvent
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
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.safe_mode import SafeModeHook
from veronica_core.shield.types import Decision, ToolCallContext

__all__ = [
    "ShieldConfig", "ShieldPipeline", "ShieldBlockedError",
    "Decision", "ToolCallContext",
    "PreDispatchHook", "EgressBoundaryHook", "RetryBoundaryHook", "BudgetBoundaryHook",
    "NoopPreDispatchHook", "NoopEgressBoundaryHook", "NoopRetryBoundaryHook", "NoopBudgetBoundaryHook",
    "SafeModeHook",
    "BudgetWindowHook",
    "TokenBudgetHook",
    "SafetyEvent",
]
