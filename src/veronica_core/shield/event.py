"""SafetyEvent dataclass for VERONICA Execution Shield.

Every non-ALLOW Decision produced by the pipeline is recorded as a
SafetyEvent so that callers can inspect what fired and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from veronica_core.shield.types import Decision


@dataclass(frozen=True)
class SafetyEvent:
    """Immutable record of a shield policy decision.

    Attributes:
        event_type: Machine-readable category, e.g. "SAFE_MODE" or
            "BUDGET_WINDOW_EXCEEDED".
        decision: The Decision that was returned (never ALLOW).
        reason: Human-readable explanation.
        hook: Class name of the hook that fired.
        request_id: Copied from ToolCallContext when available.
        ts: UTC timestamp of the event (auto-set on creation).
        metadata: Optional extra key/value pairs from the hook.
    """

    event_type: str
    decision: Decision
    reason: str
    hook: str
    request_id: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
