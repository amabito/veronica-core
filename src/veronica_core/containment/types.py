"""Data types for chain-level execution containment.

Contains all dataclasses and simple types used by
:class:`~veronica_core.containment.execution_context.ExecutionContext`.
Extracted to keep `execution_context.py` focused on the context logic.

Note: ``ExecutionContext`` itself is defined in
``veronica_core.containment.execution_context``, not here.
This module contains only the supporting data types (``CancellationToken``,
``ChainMetadata``, ``ContextSnapshot``, ``ExecutionConfig``, ``NodeRecord``,
``WrapOptions``).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

from veronica_core.shield.event import SafetyEvent

if TYPE_CHECKING:
    from veronica_core.a2a.types import AgentIdentity
    from veronica_core.partial import PartialResultBuffer
    from veronica_core.security.authority import AuthorityClaim

__all__ = [
    "CancellationToken",
    "ChainMetadata",
    "ContextSnapshot",
    "ExecutionConfig",
    "NodeRecord",
    "WrapOptions",
]


# ---------------------------------------------------------------------------
# CancellationToken
# ---------------------------------------------------------------------------


class CancellationToken:
    """Cooperative cancellation signal backed by threading.Event.

    Wrap long-running operations with ``is_cancelled`` checks or
    call ``cancel()`` to signal shutdown to all cooperating threads.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Signal cancellation. Idempotent."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """True once cancel() has been called."""
        return self._event.is_set()

    def wait(self, timeout_s: float | None = None) -> bool:
        """Block until cancelled or timeout expires.

        Returns True if cancelled, False if timeout elapsed first.
        """
        return self._event.wait(timeout=timeout_s)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainMetadata:
    """Immutable descriptor for one request chain.

    All fields except ``request_id`` and ``chain_id`` are optional so
    callers can populate only what they have available.
    """

    request_id: str
    chain_id: str
    org_id: str = ""
    team: str = ""
    service: str = ""
    user_id: str | None = None
    model: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    authority: "AuthorityClaim | None" = None

    def __post_init__(self) -> None:
        from veronica_core._utils import freeze_mapping

        freeze_mapping(self, "tags")


@dataclass(frozen=True)
class ExecutionConfig:
    """Hard limits for one chain execution.

    All numeric limits must be positive.

    Attributes:
        max_cost_usd: Chain-level USD spending ceiling. Once
            ``cost_usd_accumulated`` reaches this value, all subsequent
            wrap calls return Decision.HALT without executing the callable.
        max_steps: Maximum number of successful wrap calls. Prevents
            runaway agent loops.
        max_retries_total: Chain-wide retry budget. Counts retries across
            all nodes. Once exhausted, wrap calls return Decision.HALT.
        timeout_ms: Wall-clock timeout in milliseconds. 0 disables the
            timeout. When elapsed, the CancellationToken is signalled and
            all new wrap calls return Decision.HALT immediately.
    """

    max_cost_usd: float
    max_steps: int
    max_retries_total: int
    timeout_ms: int = 0
    budget_backend: "Any | None" = (
        None  # BudgetBackend instance for cross-process tracking
    )
    redis_url: str | None = None  # Convenience: auto-create RedisBudgetBackend

    def __post_init__(self) -> None:
        if math.isnan(self.max_cost_usd) or math.isinf(self.max_cost_usd):
            raise ValueError(
                f"max_cost_usd must be a finite number, got {self.max_cost_usd!r}"
            )
        if self.max_cost_usd < 0:
            raise ValueError(
                f"max_cost_usd must be non-negative, got {self.max_cost_usd!r}"
            )
        from veronica_core._utils import require_strict_int

        require_strict_int(self.max_steps, "max_steps")
        require_strict_int(self.max_retries_total, "max_retries_total")
        require_strict_int(self.timeout_ms, "timeout_ms")


@dataclass(frozen=True)
class WrapOptions:
    """Per-call options for wrap_llm_call / wrap_tool_call.

    All fields are optional. Omitting a field inherits the chain-level
    default from ExecutionConfig.
    """

    operation_name: str = ""
    cost_estimate_hint: float = 0.0
    timeout_ms: int | None = None
    retry_policy_override: int | None = None
    model: str | None = None
    response_hint: Any = None
    partial_buffer: "PartialResultBuffer | None" = None
    reconciliation_callback: Any = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.cost_estimate_hint):
            raise ValueError(
                f"cost_estimate_hint must be a finite number, got {self.cost_estimate_hint!r}"
            )
        if self.cost_estimate_hint < 0:
            raise ValueError(
                f"cost_estimate_hint must be non-negative, got {self.cost_estimate_hint!r}"
            )


@dataclass
class NodeRecord:
    """Record of a single LLM or tool call within the chain.

    Created at the start of each wrap call and updated when the call
    completes. Captured in ContextSnapshot.nodes.
    """

    node_id: str
    parent_id: str | None
    kind: Literal["llm", "tool", "memory_read", "memory_write"]
    operation_name: str
    start_ts: datetime
    end_ts: datetime | None
    status: Literal["ok", "halted", "aborted", "timeout", "error"]
    cost_usd: float
    retries_used: int
    partial_buffer: "PartialResultBuffer | None" = None


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable view of chain state at a point in time.

    Returned by ExecutionContext.get_snapshot(). Safe to store and
    compare across calls; all mutable state is copied on creation.
    """

    chain_id: str
    request_id: str
    step_count: int
    cost_usd_accumulated: float
    retries_used: int
    aborted: bool
    abort_reason: str | None
    elapsed_ms: float
    nodes: tuple[NodeRecord, ...]
    events: tuple[SafetyEvent, ...]
    graph_summary: Optional[dict[str, Any]] = None
    parent_chain_id: str | None = None
    agent_identity: "AgentIdentity | None" = None
    policy_metadata: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        from veronica_core._utils import freeze_mapping

        # Coerce list -> tuple for true immutability.
        if isinstance(self.nodes, list):
            object.__setattr__(self, "nodes", tuple(self.nodes))
        if isinstance(self.events, list):
            object.__setattr__(self, "events", tuple(self.events))
        if self.graph_summary is not None:
            freeze_mapping(self, "graph_summary")
        if self.policy_metadata is not None:
            freeze_mapping(self, "policy_metadata")
