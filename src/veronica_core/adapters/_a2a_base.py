"""veronica_core.adapters._a2a_base -- Shared types for A2A containment adapters.

Internal module; not part of the public API. Provides shared dataclasses,
Protocol definitions, and configuration types used by A2AClientContainmentAdapter
and A2AServerContainmentMiddleware.

Types (import directly from this module or from veronica_core.adapters._a2a_base):
    A2AMessageCost    -- cost configuration for a remote agent
    A2AResult         -- result of a contained A2A send_message call
    A2AStats          -- per-agent usage statistics
    A2AClientConfig   -- client adapter configuration
    A2AServerConfig   -- server middleware configuration
    A2AIncomingRequest -- typed request envelope for server middleware
    A2AServerDecision -- server middleware governance decision
    A2AStreamEvent    -- single event from a governed streaming response
                         (reserved for future streaming API)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from veronica_core.a2a.types import AgentIdentity, TrustLevel
from veronica_core.shield.types import Decision

logger = logging.getLogger(__name__)

# Hard cap on distinct agent IDs tracked in stats.  Beyond this limit new
# agent IDs are silently dropped to prevent DoS via attacker-controlled
# agent-ID generation.  Matches _STATS_WARN_LIMIT in _mcp_base.py.
_STATS_WARN_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Protocol for a2a-sdk client (type-safe without hard import)
# ---------------------------------------------------------------------------


@runtime_checkable
class A2AClientProtocol(Protocol):
    """Structural type for a2a-sdk A2AClient.

    Allows type-safe usage without importing a2a-sdk at runtime.
    Any object with matching methods satisfies this protocol.
    """

    async def send_message(self, request: Any) -> Any:
        """Send a message to a remote A2A agent."""
        ...


# ---------------------------------------------------------------------------
# Cost configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AMessageCost:
    """Cost configuration for messages sent to a specific remote agent.

    Attributes:
        agent_id: Remote agent identifier (used as stats key).
        cost_per_message: Fixed USD cost charged per send_message invocation.
        cost_per_token: Variable USD cost charged per token in the response.
    """

    agent_id: str
    cost_per_message: float = 0.01
    cost_per_token: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.cost_per_message, bool):
            raise TypeError(
                f"cost_per_message must be a finite number, got {self.cost_per_message!r}"
            )
        if not isinstance(self.cost_per_message, (int, float)) or not math.isfinite(
            self.cost_per_message
        ):
            raise ValueError(
                f"cost_per_message must be a finite number, got {self.cost_per_message!r}"
            )
        if self.cost_per_message < 0:
            raise ValueError(
                f"cost_per_message must be >= 0, got {self.cost_per_message}"
            )
        if isinstance(self.cost_per_token, bool):
            raise TypeError(
                f"cost_per_token must be a finite number, got {self.cost_per_token!r}"
            )
        if not isinstance(self.cost_per_token, (int, float)) or not math.isfinite(
            self.cost_per_token
        ):
            raise ValueError(
                f"cost_per_token must be a finite number, got {self.cost_per_token!r}"
            )
        if self.cost_per_token < 0:
            raise ValueError(
                f"cost_per_token must be >= 0, got {self.cost_per_token}"
            )


# ---------------------------------------------------------------------------
# Client adapter configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AClientConfig:
    """Configuration for A2AClientContainmentAdapter.

    Attributes:
        default_cost_per_message: Cost applied to agents without explicit
            A2AMessageCost entry. Must be >= 0.
        timeout_seconds: Per-call timeout for send_message. None = no timeout.
        max_poll_attempts: Maximum GetTask polling attempts per task.
            Reserved for future polling API.
        max_state_transitions: Maximum INPUT_REQUIRED round-trips per task
            before fail-closed. Reserved for future state machine API.
        max_stream_chunks: Maximum chunks in a streaming response.
            Reserved for future streaming API.
        max_stream_bytes: Maximum total bytes in a streaming response.
            Reserved for future streaming API.
        max_stream_duration_s: Maximum wall-clock seconds for a streaming
            response before forced termination.
            Reserved for future streaming API.
        stats_cap: Maximum distinct agent IDs tracked in stats.
    """

    default_cost_per_message: float = 0.01
    timeout_seconds: float | None = 30.0
    max_poll_attempts: int = 100
    max_state_transitions: int = 8
    max_stream_chunks: int = 10_000
    max_stream_bytes: int = 10_485_760  # 10 MB
    max_stream_duration_s: float = 300.0
    stats_cap: int = _STATS_WARN_LIMIT

    def __post_init__(self) -> None:
        if isinstance(self.default_cost_per_message, bool):
            raise TypeError(
                f"default_cost_per_message must be a finite number, "
                f"got {self.default_cost_per_message!r}"
            )
        if not isinstance(self.default_cost_per_message, (int, float)) or not math.isfinite(
            self.default_cost_per_message
        ):
            raise ValueError(
                f"default_cost_per_message must be a finite number, "
                f"got {self.default_cost_per_message!r}"
            )
        if self.default_cost_per_message < 0:
            raise ValueError(
                f"default_cost_per_message must be >= 0, got {self.default_cost_per_message}"
            )
        if self.timeout_seconds is not None:
            if isinstance(self.timeout_seconds, bool):
                raise TypeError(
                    f"timeout_seconds must be a finite number or None, "
                    f"got {self.timeout_seconds!r}"
                )
            if not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(
                self.timeout_seconds
            ):
                raise ValueError(
                    f"timeout_seconds must be a finite number or None, "
                    f"got {self.timeout_seconds!r}"
                )
            if self.timeout_seconds <= 0:
                raise ValueError(
                    f"timeout_seconds must be > 0 or None, got {self.timeout_seconds}"
                )
        if not isinstance(self.max_poll_attempts, int) or isinstance(self.max_poll_attempts, bool):
            raise TypeError(
                f"max_poll_attempts must be int, got {type(self.max_poll_attempts).__name__}"
            )
        if self.max_poll_attempts <= 0:
            raise ValueError(
                f"max_poll_attempts must be > 0, got {self.max_poll_attempts}"
            )
        if not isinstance(self.max_state_transitions, int) or isinstance(
            self.max_state_transitions, bool
        ):
            raise TypeError(
                f"max_state_transitions must be int, got {type(self.max_state_transitions).__name__}"
            )
        if self.max_state_transitions <= 0:
            raise ValueError(
                f"max_state_transitions must be > 0, got {self.max_state_transitions}"
            )
        if not isinstance(self.max_stream_chunks, int) or isinstance(self.max_stream_chunks, bool):
            raise TypeError(
                f"max_stream_chunks must be int, got {type(self.max_stream_chunks).__name__}"
            )
        if self.max_stream_chunks <= 0:
            raise ValueError(
                f"max_stream_chunks must be > 0, got {self.max_stream_chunks}"
            )
        if not isinstance(self.max_stream_bytes, int) or isinstance(self.max_stream_bytes, bool):
            raise TypeError(
                f"max_stream_bytes must be int, got {type(self.max_stream_bytes).__name__}"
            )
        if self.max_stream_bytes <= 0:
            raise ValueError(
                f"max_stream_bytes must be > 0, got {self.max_stream_bytes}"
            )
        if isinstance(self.max_stream_duration_s, bool):
            raise TypeError(
                f"max_stream_duration_s must be a finite number, "
                f"got {self.max_stream_duration_s!r}"
            )
        if not isinstance(self.max_stream_duration_s, (int, float)) or not math.isfinite(
            self.max_stream_duration_s
        ):
            raise ValueError(
                f"max_stream_duration_s must be a finite number, "
                f"got {self.max_stream_duration_s!r}"
            )
        if self.max_stream_duration_s <= 0:
            raise ValueError(
                f"max_stream_duration_s must be > 0, got {self.max_stream_duration_s}"
            )
        if not isinstance(self.stats_cap, int) or isinstance(self.stats_cap, bool):
            raise TypeError(
                f"stats_cap must be int, got {type(self.stats_cap).__name__}"
            )
        if self.stats_cap <= 0:
            raise ValueError(
                f"stats_cap must be > 0, got {self.stats_cap}"
            )


# ---------------------------------------------------------------------------
# Server middleware configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AServerConfig:
    """Configuration for A2AServerContainmentMiddleware.

    Attributes:
        max_message_size_bytes: Hard limit on incoming message payload size.
        max_requests_per_minute_per_tenant: Tenant-global rate limit bucket.
        max_requests_per_minute_per_sender: Per-sender rate limit bucket
            (within a tenant).
        fail_closed: When True, deny requests if no governance hooks match.
    """

    max_message_size_bytes: int = 1_048_576  # 1 MB
    max_requests_per_minute_per_tenant: int = 600
    max_requests_per_minute_per_sender: int = 60
    fail_closed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.max_message_size_bytes, int) or isinstance(
            self.max_message_size_bytes, bool
        ):
            raise TypeError(
                f"max_message_size_bytes must be int, "
                f"got {type(self.max_message_size_bytes).__name__}"
            )
        if self.max_message_size_bytes <= 0:
            raise ValueError(
                f"max_message_size_bytes must be > 0, got {self.max_message_size_bytes}"
            )
        if not isinstance(self.max_requests_per_minute_per_tenant, int) or isinstance(
            self.max_requests_per_minute_per_tenant, bool
        ):
            raise TypeError(
                f"max_requests_per_minute_per_tenant must be int, "
                f"got {type(self.max_requests_per_minute_per_tenant).__name__}"
            )
        if self.max_requests_per_minute_per_tenant <= 0:
            raise ValueError(
                f"max_requests_per_minute_per_tenant must be > 0, "
                f"got {self.max_requests_per_minute_per_tenant}"
            )
        if not isinstance(self.max_requests_per_minute_per_sender, int) or isinstance(
            self.max_requests_per_minute_per_sender, bool
        ):
            raise TypeError(
                f"max_requests_per_minute_per_sender must be int, "
                f"got {type(self.max_requests_per_minute_per_sender).__name__}"
            )
        if self.max_requests_per_minute_per_sender <= 0:
            raise ValueError(
                f"max_requests_per_minute_per_sender must be > 0, "
                f"got {self.max_requests_per_minute_per_sender}"
            )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AResult:
    """Result of a contained outbound A2A send_message call.

    Attributes:
        success: True when the remote agent returned a valid response.
        task: A2A Task object returned by the remote agent, if any.
        message: A2A Message response, if the agent replied directly.
        error: Human-readable error string. Never contains exception class
            names or internal details (Rule 5: external errors generic).
        decision: ALLOW when the call was permitted and executed; HALT when
            blocked by budget, circuit breaker, or governance.
        cost_usd: Actual USD cost charged for this call.
        trust_level: Trust level of the remote agent at time of call.
    """

    success: bool
    task: Any = None
    message: Any = None
    error: Optional[str] = None
    decision: Decision = Decision.ALLOW
    cost_usd: float = 0.0
    trust_level: TrustLevel | None = None


@dataclass
class A2AStats:
    """Per-agent usage statistics.

    Attributes:
        agent_id: Remote agent identifier.
        message_count: Total send_message invocations attempted.
        total_cost_usd: Cumulative cost across all successful calls.
        error_count: Number of invocations that raised or returned error.
        avg_latency_ms: Rolling average latency of successful calls.
        trust_level: Current trust level for this agent.
    """

    agent_id: str
    message_count: int = 0
    total_cost_usd: float = 0.0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    trust_level: TrustLevel = TrustLevel.UNTRUSTED

    # Internal; not part of public summary.
    _total_latency_ms: float = field(default=0.0, repr=False)
    _latency_sample_count: int = field(default=0, repr=False)


# ---------------------------------------------------------------------------
# Server middleware types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AIncomingRequest:
    """Typed request envelope for server middleware.

    Represents an incoming A2A operation before it reaches the agent handler.

    Attributes:
        operation: A2A operation name ("SendMessage", "SendStreamingMessage",
            "GetTask", "CancelTask", "ListTasks", "SubscribeToTask").
        tenant_id: Tenant scope from the A2A request.
        sender_identity: Identity of the calling agent.
        message: A2A Message payload (for SendMessage operations).
        task_id: Task identifier (for GetTask/CancelTask/SubscribeToTask).
        agent_card: Sender's Agent Card dict (for trust verification).
        content_size_bytes: Pre-computed payload size in bytes for
            governance hooks. 0 if not applicable.
    """

    operation: str
    tenant_id: str
    sender_identity: AgentIdentity
    message: Any = None
    task_id: str | None = None
    agent_card: dict[str, Any] | None = None
    content_size_bytes: int = 0

    _VALID_OPERATIONS: frozenset[str] = field(
        default=frozenset({
            "SendMessage",
            "SendStreamingMessage",
            "GetTask",
            "CancelTask",
            "ListTasks",
            "SubscribeToTask",
        }),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str):
            raise TypeError(
                f"A2AIncomingRequest.operation must be str, "
                f"got {type(self.operation).__name__}"
            )
        if self.operation not in self._VALID_OPERATIONS:
            raise ValueError(
                f"A2AIncomingRequest.operation={self.operation!r} is invalid; "
                f"valid: {sorted(self._VALID_OPERATIONS)}"
            )
        if not self.tenant_id or not self.tenant_id.strip():
            raise ValueError("A2AIncomingRequest.tenant_id must not be empty or whitespace")
        if not isinstance(self.content_size_bytes, int) or isinstance(
            self.content_size_bytes, bool
        ):
            raise TypeError(
                f"content_size_bytes must be int, got {type(self.content_size_bytes).__name__}"
            )
        if self.content_size_bytes < 0:
            raise ValueError(
                f"content_size_bytes must be >= 0, got {self.content_size_bytes}"
            )


_VALID_VERDICTS = frozenset({"ALLOW", "DENY", "DEGRADE"})


@dataclass(frozen=True)
class A2AServerDecision:
    """Result of server middleware governance evaluation.

    Attributes:
        verdict: Governance outcome -- "ALLOW", "DENY", or "DEGRADE".
        reason: Human-readable explanation (no internal details).
        sender_trust: Trust level assigned to the sender.
        degrade_directive: If verdict is DEGRADE, the directive to apply.
    """

    verdict: str
    reason: str
    sender_trust: TrustLevel
    degrade_directive: Any = None

    def __post_init__(self) -> None:
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {self.verdict!r}"
            )


@dataclass(frozen=True)
class A2AStreamEvent:
    """Single event from a governed streaming response.

    Wraps an A2A stream event with containment metadata.

    Attributes:
        event_type: "status_update", "artifact_update", or "message".
        payload: Raw event payload from the A2A stream.
        decision: Governance decision applied to this event.
        chunk_index: Zero-based index of this chunk in the stream.
        cumulative_bytes: Total bytes received so far in this stream.
    """

    event_type: str
    payload: Any
    decision: Decision = Decision.ALLOW
    chunk_index: int = 0
    cumulative_bytes: int = 0

    _VALID_EVENT_TYPES: frozenset[str] = field(
        default=frozenset({"status_update", "artifact_update", "message"}),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.event_type not in self._VALID_EVENT_TYPES:
            raise ValueError(
                f"A2AStreamEvent.event_type={self.event_type!r} is invalid; "
                f"valid: {sorted(self._VALID_EVENT_TYPES)}"
            )
        if not isinstance(self.chunk_index, int) or isinstance(self.chunk_index, bool):
            raise TypeError(
                f"chunk_index must be int, got {type(self.chunk_index).__name__}"
            )
        if self.chunk_index < 0:
            raise ValueError(f"chunk_index must be >= 0, got {self.chunk_index}")
        if not isinstance(self.cumulative_bytes, int) or isinstance(
            self.cumulative_bytes, bool
        ):
            raise TypeError(
                f"cumulative_bytes must be int, got {type(self.cumulative_bytes).__name__}"
            )
        if self.cumulative_bytes < 0:
            raise ValueError(
                f"cumulative_bytes must be >= 0, got {self.cumulative_bytes}"
            )
