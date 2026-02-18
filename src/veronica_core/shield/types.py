"""Core shield types for VERONICA Execution Shield.

Decision enum: possible outcomes of a shield policy check.
ToolCallContext: immutable snapshot of a single tool invocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    """Outcome of a shield policy evaluation."""

    ALLOW = "ALLOW"
    RETRY = "RETRY"
    HALT = "HALT"
    DEGRADE = "DEGRADE"
    QUARANTINE = "QUARANTINE"
    QUEUE = "QUEUE"


@dataclass(frozen=True)
class ToolCallContext:
    """Immutable snapshot describing a single tool invocation.

    All fields except ``request_id`` are optional so callers can
    populate only what they have available.
    """

    request_id: str
    user_id: str | None = None
    session_id: str | None = None
    tool_name: str | None = None
    model: str | None = None
    endpoint: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
