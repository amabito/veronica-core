"""VERONICA kernel package -- core governance primitives.

Exports:
- DecisionEnvelope: attestation wrapper for governance decisions (opt-in per path)
- ReasonCode: machine-readable reason codes
- make_envelope: factory for DecisionEnvelope with auto-generated audit fields
- ReservationState: lifecycle states for resource reservations (HA ABI, v3.5.0)
- PolicyEpochStamp: immutable stamp identifying the policy epoch at a point in time
- BreakerReflection: read-only snapshot of a CircuitBreaker's observable state
- Reservation: immutable receive slot representing a resource reservation
- HeartbeatSnapshot: point-in-time snapshot of a kernel instance for HA observation
"""

from veronica_core.kernel.decision import DecisionEnvelope, ReasonCode, make_envelope
from veronica_core.kernel.ha import (
    BreakerReflection,
    HeartbeatSnapshot,
    PolicyEpochStamp,
    Reservation,
    ReservationState,
)

__all__ = [
    "DecisionEnvelope",
    "ReasonCode",
    "make_envelope",
    "ReservationState",
    "PolicyEpochStamp",
    "BreakerReflection",
    "Reservation",
    "HeartbeatSnapshot",
]
