"""DecisionEnvelope -- unified attestation wrapper for all governance decisions.

Every governance outcome (budget, circuit breaker, memory, shield, policy)
is wrapped in a DecisionEnvelope before being returned to callers. The
envelope carries mandatory audit fields so that every decision can be traced
to a policy, an issuer, and a point in time.

No reasoning, no AI, no sandbox, no policy authoring is implemented here.
This module is pure ABI -- types and factories only.
"""

from __future__ import annotations

__all__ = [
    "DecisionEnvelope",
    "ReasonCode",
    "make_envelope",
]

import re
import time
import types as _types
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

# Regex for UUID4 format validation (8-4-4-4-12 hex).
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Decision values that are recognised by DecisionEnvelope.
_KNOWN_DECISIONS: frozenset[str] = frozenset(
    {"ALLOW", "DENY", "HALT", "DEGRADE", "QUARANTINE", "RETRY", "QUEUE"}
)

# Decisions that are considered "allowed" (operation may proceed).
_ALLOWED_DECISIONS: frozenset[str] = frozenset(
    {"ALLOW", "DEGRADE", "QUARANTINE", "QUEUE", "RETRY"}
)

# Decisions that are considered "denied" (operation must not proceed).
_DENIED_DECISIONS: frozenset[str] = frozenset({"DENY", "HALT"})


class ReasonCode(str, Enum):
    """Machine-readable reason codes for governance decisions.

    These codes are stable identifiers that downstream systems can
    pattern-match against without parsing human-readable reason strings.
    """

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    STEP_LIMIT = "STEP_LIMIT"
    RETRY_BUDGET = "RETRY_BUDGET"
    TIMEOUT = "TIMEOUT"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    MEMORY_GOVERNANCE_DENIED = "MEMORY_GOVERNANCE_DENIED"
    MEMORY_GOVERNANCE_QUARANTINE = "MEMORY_GOVERNANCE_QUARANTINE"
    POLICY_UNSIGNED = "POLICY_UNSIGNED"
    POLICY_HASH_MISMATCH = "POLICY_HASH_MISMATCH"
    POLICY_EPOCH_ROLLBACK = "POLICY_EPOCH_ROLLBACK"
    ABORTED = "ABORTED"
    SAFE_MODE = "SAFE_MODE"
    SHELL_BLOCKED = "SHELL_BLOCKED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    FILE_BLOCKED = "FILE_BLOCKED"
    TRUST_VIOLATION = "TRUST_VIOLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DecisionEnvelope:
    """Unified attestation wrapper for all governance decisions.

    Every governance outcome -- budget, circuit breaker, memory governance,
    execution shield, policy pipeline -- is wrapped in a DecisionEnvelope
    before being returned to callers. The envelope provides a mandatory
    audit trail: policy hash, reason code, unique audit ID, and timestamp.

    Fields:
        decision: Categorical outcome. One of ALLOW, DENY, HALT, DEGRADE,
            QUARANTINE, RETRY, QUEUE.
        policy_hash: SHA-256 hex digest of the active PolicyBundle at the
            moment of evaluation. Empty string when no policy bundle is active.
        reason_code: Machine-readable reason from ReasonCode (or a custom
            string for extension points).
        reason: Human-readable explanation of the decision.
        audit_id: UUID4 string, generated at creation. Never empty.
        timestamp: Unix epoch float from time.time() at creation.
        policy_epoch: Monotonic epoch counter from PolicyMetadata. 0 when
            no policy bundle is active.
        issuer: Name of the component that produced this decision, e.g.
            "BudgetEnforcer", "CircuitBreaker", "MemoryGovernanceHook".
        metadata: Arbitrary key/value pairs from the issuer. Frozen via
            MappingProxyType to prevent post-construction mutation.
    """

    decision: str
    policy_hash: str
    reason_code: str
    reason: str
    audit_id: str
    timestamp: float
    policy_epoch: int
    issuer: str
    metadata: dict[str, Any] = field(default_factory=dict)

    # Fields that must not appear as metadata keys (prevents overwrite in to_audit_dict).
    _RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"decision", "policy_hash", "reason_code", "reason", "audit_id",
         "timestamp", "policy_epoch", "issuer"}
    )

    def __post_init__(self) -> None:
        if self.decision not in _KNOWN_DECISIONS:
            raise ValueError(
                f"DecisionEnvelope.decision must be one of {sorted(_KNOWN_DECISIONS)}, "
                f"got {self.decision!r}"
            )
        if not self.audit_id:
            raise ValueError("DecisionEnvelope.audit_id must be non-empty")
        # H3: Validate audit_id is a valid UUID4 string and normalise to lower-case.
        if not _UUID4_RE.match(self.audit_id):
            raise ValueError(
                f"DecisionEnvelope.audit_id must be a valid UUID4 string, "
                f"got {self.audit_id!r}"
            )
        lower_id = self.audit_id.lower()
        if lower_id != self.audit_id:
            object.__setattr__(self, "audit_id", lower_id)
        # C1: Reject metadata keys that collide with core audit fields.
        collisions = set(self.metadata) & self._RESERVED_KEYS
        if collisions:
            raise ValueError(
                f"DecisionEnvelope.metadata contains reserved keys: {sorted(collisions)}"
            )
        # Freeze mutable metadata to prevent post-construction mutation.
        object.__setattr__(
            self, "metadata", _types.MappingProxyType(dict(self.metadata))
        )

    @property
    def allowed(self) -> bool:
        """True when the operation may proceed.

        ALLOW, DEGRADE, QUARANTINE, QUEUE, and RETRY are all considered
        allowed -- the operation proceeds, possibly with constraints.
        """
        return self.decision in _ALLOWED_DECISIONS

    @property
    def denied(self) -> bool:
        """True when the operation must not proceed.

        DENY and HALT are the two denial decisions. HALT additionally
        signals that the entire agent run should terminate.
        """
        return self.decision in _DENIED_DECISIONS

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict suitable for audit log emission.

        All fields are included. metadata is expanded inline (shallow copy).
        """
        result: dict[str, Any] = {
            "decision": self.decision,
            "policy_hash": self.policy_hash,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "policy_epoch": self.policy_epoch,
            "issuer": self.issuer,
        }
        result.update(self.metadata)
        return result


def make_envelope(
    decision: str,
    reason_code: str | ReasonCode,
    reason: str,
    issuer: str,
    policy_hash: str = "",
    policy_epoch: int = 0,
    metadata: dict[str, Any] | None = None,
) -> DecisionEnvelope:
    """Factory for DecisionEnvelope with auto-generated audit fields.

    Generates a UUID4 audit_id and records the current time.time() as
    the timestamp. All other fields are passed through.

    Args:
        decision: Categorical outcome (ALLOW, DENY, HALT, DEGRADE,
            QUARANTINE, RETRY, QUEUE).
        reason_code: Machine-readable code. Accepts ReasonCode enum
            members or raw strings for custom extension points.
        reason: Human-readable explanation.
        issuer: Name of the component producing this decision.
        policy_hash: SHA-256 hex of the active PolicyBundle. Defaults
            to empty string when no bundle is active.
        policy_epoch: Monotonic epoch from PolicyMetadata. Defaults to 0.
        metadata: Optional extra key/value pairs. Copied shallowly before
            being frozen into MappingProxyType.

    Returns:
        A frozen DecisionEnvelope with a unique audit_id and current timestamp.

    Raises:
        ValueError: If decision is not a recognised value or audit_id is
            empty (should not happen via this factory).
    """
    code_str = reason_code.value if isinstance(reason_code, ReasonCode) else reason_code
    return DecisionEnvelope(
        decision=decision,
        policy_hash=policy_hash,
        reason_code=code_str,
        reason=reason,
        audit_id=str(uuid.uuid4()),
        timestamp=time.time(),
        policy_epoch=policy_epoch,
        issuer=issuer,
        metadata=metadata or {},
    )
