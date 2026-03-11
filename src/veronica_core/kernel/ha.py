"""HA minimal ABI -- receive slots and snapshot types for future HA integration.

These are pure data types only. No HA logic, no consensus, no peer governance,
no federation. Authoritative state is ALWAYS external. This module defines the
minimum surface needed so that future HA components can observe kernel state
without coupling to internal implementation details.

No reasoning, no AI, no sandbox, no policy authoring is implemented here.
"""

from __future__ import annotations

__all__ = [
    "ReservationState",
    "PolicyEpochStamp",
    "BreakerReflection",
    "Reservation",
    "HeartbeatSnapshot",
]

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from veronica_core._utils import freeze_mapping


class ReservationState(str, Enum):
    """Lifecycle states for a resource reservation."""

    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PolicyEpochStamp:
    """Immutable stamp identifying the policy epoch at a point in time.

    Used to correlate decisions and snapshots to a specific policy version.
    Authoritative epoch sequence is maintained by an external policy store.

    Fields:
        epoch: Monotonic epoch counter. Must be >= 0.
        policy_hash: SHA-256 hex digest of the policy bundle in effect.
        issuer: Component that issued this stamp. Empty string if unknown.
        timestamp: Unix epoch float recorded at creation.
    """

    epoch: int
    policy_hash: str
    issuer: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.epoch < 0:
            raise ValueError(f"PolicyEpochStamp.epoch must be >= 0, got {self.epoch}")


@dataclass(frozen=True)
class BreakerReflection:
    """Read-only snapshot of a CircuitBreaker's observable state.

    Produced by CircuitBreaker.reflect(). Contains only the fields needed
    for external observation and audit. Does not expose lock internals or
    allow state mutation.

    Fields:
        breaker_id: Identifier of the breaker (bound owner_id, or empty).
        state: Circuit state string -- "CLOSED", "OPEN", or "HALF_OPEN".
        failure_count: Consecutive failure count at snapshot time.
        success_count: Total success count at snapshot time.
        last_failure_ts: monotonic timestamp of last failure; 0.0 if none.
        last_success_ts: monotonic timestamp of last success; 0.0 if none.
        recovery_timeout: Configured recovery timeout in seconds.
        failure_threshold: Configured failure threshold.
        timestamp: Unix epoch float recorded at snapshot time.
    """

    breaker_id: str
    state: str
    failure_count: int
    success_count: int
    last_failure_ts: float
    last_success_ts: float
    recovery_timeout: float
    failure_threshold: int
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.failure_count < 0:
            raise ValueError(
                f"BreakerReflection.failure_count must be >= 0, got {self.failure_count}"
            )
        if self.success_count < 0:
            raise ValueError(
                f"BreakerReflection.success_count must be >= 0, got {self.success_count}"
            )


@dataclass(frozen=True)
class Reservation:
    """Immutable receive slot representing a resource reservation request.

    Reservation objects are created by callers that need to reserve a
    resource quantity (tokens, budget, capacity) before committing. The
    authoritative state of a reservation is maintained externally; this
    object is a point-in-time snapshot of the caller's intent.

    Fields:
        reservation_id: Non-empty unique identifier for this reservation.
        resource_type: Non-empty string identifying the resource class.
        amount: Non-negative quantity being reserved.
        state: Lifecycle state. Defaults to PENDING.
        epoch_stamp: Optional policy epoch at reservation creation time.
        created_at: Unix epoch float at creation.
        expires_at: Unix epoch float when this reservation expires.
            0.0 means no expiry.
        metadata: Arbitrary key/value context. Frozen via MappingProxyType.

    Properties:
        is_expired: True when expires_at > 0 and current time has passed it.
        is_active: True when state is PENDING or COMMITTED and not expired.
    """

    reservation_id: str
    resource_type: str
    amount: float
    state: ReservationState = ReservationState.PENDING
    epoch_stamp: PolicyEpochStamp | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reservation_id:
            raise ValueError("Reservation.reservation_id must be non-empty")
        if not self.resource_type:
            raise ValueError("Reservation.resource_type must be non-empty")
        if math.isnan(self.amount) or self.amount < 0:
            raise ValueError(f"Reservation.amount must be >= 0, got {self.amount}")
        # Freeze mutable metadata dict to prevent post-construction mutation.
        freeze_mapping(self, "metadata")

    @property
    def is_expired(self) -> bool:
        """True when the reservation has a finite expiry that has passed."""
        if self.expires_at == 0.0:
            return False
        return time.time() > self.expires_at

    @property
    def is_active(self) -> bool:
        """True when the reservation may still be committed.

        A reservation is active when its state is PENDING or COMMITTED
        and it has not yet expired.
        """
        if self.state not in (ReservationState.PENDING, ReservationState.COMMITTED):
            return False
        return not self.is_expired

    def commit(self) -> "Reservation":
        """Transition to COMMITTED state. Returns a new Reservation instance.

        Only valid from PENDING state. Raises ValueError if current state
        is not PENDING or if reservation is expired.
        """
        if self.state != ReservationState.PENDING:
            raise ValueError(
                f"Cannot commit reservation in {self.state.value} state; "
                f"only PENDING reservations can be committed"
            )
        if self.is_expired:
            raise ValueError("Cannot commit expired reservation")
        return Reservation(
            reservation_id=self.reservation_id,
            resource_type=self.resource_type,
            amount=self.amount,
            state=ReservationState.COMMITTED,
            epoch_stamp=self.epoch_stamp,
            created_at=self.created_at,
            expires_at=self.expires_at,
            metadata=dict(self.metadata),
        )

    def rollback(self) -> "Reservation":
        """Transition to ROLLED_BACK state. Returns a new Reservation instance.

        Valid from PENDING or COMMITTED state. Raises ValueError if already
        rolled back or expired (terminal states).
        """
        terminal = {ReservationState.ROLLED_BACK, ReservationState.EXPIRED}
        if self.state in terminal:
            raise ValueError(
                f"Cannot rollback reservation in {self.state.value} state; "
                f"reservation is already in a terminal state"
            )
        return Reservation(
            reservation_id=self.reservation_id,
            resource_type=self.resource_type,
            amount=self.amount,
            state=ReservationState.ROLLED_BACK,
            epoch_stamp=self.epoch_stamp,
            created_at=self.created_at,
            expires_at=self.expires_at,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class HeartbeatSnapshot:
    """Point-in-time snapshot of a kernel instance for external observation.

    Emitted periodically so that HA components can monitor kernel liveness
    and state without accessing kernel internals. Authoritative state is
    always held externally; this snapshot is best-effort and may lag.

    Fields:
        kernel_id: Unique identifier of the kernel instance.
        sequence: Monotonic heartbeat sequence number. Must be >= 0.
        epoch_stamp: Policy epoch at snapshot time. None if no policy active.
        breakers: Tuple of BreakerReflection snapshots (one per breaker).
        active_reservations: Count of reservations in PENDING or COMMITTED state.
        active_chains: Count of active execution chains.
        total_decisions: Cumulative governance decision count since startup.
        uptime_seconds: Seconds since kernel startup.
        timestamp: Unix epoch float recorded at snapshot time.
        metadata: Arbitrary key/value context. Frozen via MappingProxyType.

    Methods:
        to_audit_dict(): Flat dict representation suitable for audit log emission.
    """

    kernel_id: str
    sequence: int
    epoch_stamp: PolicyEpochStamp | None = None
    breakers: tuple[BreakerReflection, ...] = ()
    active_reservations: int = 0
    active_chains: int = 0
    total_decisions: int = 0
    uptime_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Fields that must not appear as metadata keys (prevents overwrite in to_audit_dict).
    _RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"kernel_id", "sequence", "active_reservations", "active_chains",
         "total_decisions", "uptime_seconds", "timestamp", "breaker_count",
         "breakers", "epoch_epoch", "epoch_policy_hash", "epoch_issuer",
         "epoch_timestamp"}
    )

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError(
                f"HeartbeatSnapshot.sequence must be >= 0, got {self.sequence}"
            )
        # C1: Reject metadata keys that collide with core audit fields.
        collisions = set(self.metadata) & self._RESERVED_KEYS
        if collisions:
            raise ValueError(
                f"HeartbeatSnapshot.metadata contains reserved keys: {sorted(collisions)}"
            )
        # Coerce list to tuple for breakers to ensure the frozen invariant
        # holds even when callers pass a list.
        if not isinstance(self.breakers, tuple):
            object.__setattr__(self, "breakers", tuple(self.breakers))
        # Freeze mutable metadata dict to prevent post-construction mutation.
        freeze_mapping(self, "metadata")

    @classmethod
    def capture(
        cls,
        kernel_id: str,
        sequence: int,
        epoch_stamp: PolicyEpochStamp | None = None,
        breakers: tuple[BreakerReflection, ...] = (),
        active_reservations: int = 0,
        active_chains: int = 0,
        total_decisions: int = 0,
        uptime_seconds: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> "HeartbeatSnapshot":
        """Factory for HeartbeatSnapshot with current timestamp.

        Used by HA components to capture kernel state at a point in time.
        The snapshot is a best-effort observation; authoritative state is
        always held externally.
        """
        return cls(
            kernel_id=kernel_id,
            sequence=sequence,
            epoch_stamp=epoch_stamp,
            breakers=breakers,
            active_reservations=active_reservations,
            active_chains=active_chains,
            total_decisions=total_decisions,
            uptime_seconds=uptime_seconds,
            timestamp=time.time(),
            metadata=metadata or {},
        )

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialize snapshot to a flat dict suitable for audit log emission.

        breakers are serialized as a list of dicts. metadata is expanded
        inline (shallow copy). epoch_stamp fields are inlined with an
        'epoch_' prefix when present.
        """
        result: dict[str, Any] = {
            "kernel_id": self.kernel_id,
            "sequence": self.sequence,
            "active_reservations": self.active_reservations,
            "active_chains": self.active_chains,
            "total_decisions": self.total_decisions,
            "uptime_seconds": self.uptime_seconds,
            "timestamp": self.timestamp,
            "breaker_count": len(self.breakers),
            "breakers": [
                {
                    "breaker_id": b.breaker_id,
                    "state": b.state,
                    "failure_count": b.failure_count,
                    "success_count": b.success_count,
                    "last_failure_ts": b.last_failure_ts,
                    "last_success_ts": b.last_success_ts,
                    "recovery_timeout": b.recovery_timeout,
                    "failure_threshold": b.failure_threshold,
                    "timestamp": b.timestamp,
                }
                for b in self.breakers
            ],
        }
        if self.epoch_stamp is not None:
            result["epoch_epoch"] = self.epoch_stamp.epoch
            result["epoch_policy_hash"] = self.epoch_stamp.policy_hash
            result["epoch_issuer"] = self.epoch_stamp.issuer
            result["epoch_timestamp"] = self.epoch_stamp.timestamp
        result.update(self.metadata)
        return result
