"""Memory Governance ABI -- core types for governing memory operations.

This module defines the type vocabulary for memory governance:
- MemoryAction: what kind of operation is requested
- MemoryProvenance: trust level of stored content
- MemoryOperation: fully-described memory operation request
- MemoryPolicyContext: ambient context for policy evaluation
- GovernanceVerdict: categorical outcome of a governance decision
- MemoryGovernanceDecision: full decision record with audit trail

No memory backend or storage is implemented here.
"""

from __future__ import annotations

__all__ = [
    "MemoryAction",
    "MemoryProvenance",
    "MemoryOperation",
    "MemoryPolicyContext",
    "GovernanceVerdict",
    "MemoryGovernanceDecision",
]

import time
import types as _types
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from veronica_core.kernel.decision import DecisionEnvelope


class MemoryAction(str, Enum):
    """Type of memory operation being requested."""

    READ = "read"
    WRITE = "write"
    RETRIEVE = "retrieve"
    ARCHIVE = "archive"
    CONSOLIDATE = "consolidate"
    DELETE = "delete"
    QUARANTINE = "quarantine"


class MemoryProvenance(str, Enum):
    """Trust classification of memory content."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    QUARANTINED = "quarantined"
    UNKNOWN = "unknown"


class GovernanceVerdict(str, Enum):
    """Categorical outcome of a memory governance evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    QUARANTINE = "quarantine"
    DEGRADE = "degrade"


@dataclass(frozen=True)
class MemoryOperation:
    """Fully-described memory operation submitted for governance evaluation.

    All fields are immutable. Callers build a new instance for each request.
    content_size_bytes must be >= 0. action must be a MemoryAction instance.
    """

    action: MemoryAction
    resource_id: str = ""
    agent_id: str = ""
    namespace: str = ""
    content_hash: str = ""
    content_size_bytes: int = 0
    provenance: MemoryProvenance = MemoryProvenance.UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not isinstance(self.action, MemoryAction):
            raise TypeError(
                f"MemoryOperation.action must be a MemoryAction, got {type(self.action)!r}"
            )
        if self.content_size_bytes < 0:
            raise ValueError(
                f"MemoryOperation.content_size_bytes must be >= 0, "
                f"got {self.content_size_bytes}"
            )
        # Freeze mutable metadata to prevent post-construction mutation.
        object.__setattr__(
            self, "metadata", _types.MappingProxyType(dict(self.metadata))
        )


@dataclass(frozen=True)
class MemoryPolicyContext:
    """Ambient context passed to governance hooks during evaluation.

    Carries chain-level counters and metadata that hooks can use
    to make rate-aware or quota-aware decisions.
    """

    operation: MemoryOperation
    chain_id: str = ""
    request_id: str = ""
    trust_level: str = ""
    total_memory_ops_in_chain: int = 0
    total_bytes_written_in_chain: int = 0
    active_quarantine_count: int = 0


@dataclass(frozen=True)
class MemoryGovernanceDecision:
    """Full governance decision record, suitable for audit trail emission.

    verdict determines whether the operation proceeds:
    - ALLOW: proceed normally
    - DENY: reject the operation
    - QUARANTINE: allow but mark content as quarantined
    - DEGRADE: allow with reduced capability or rate-limited

    QUARANTINE and DEGRADE are treated as "allowed" by the allowed property
    so callers can still proceed while downstream systems track the verdict.
    """

    # Fields that must not appear as audit_metadata keys (prevents overwrite in to_audit_dict).
    _RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"verdict", "reason", "policy_id", "operation_action",
         "operation_resource_id", "operation_agent_id", "operation_namespace",
         "operation_provenance", "operation_content_size_bytes"}
    )

    verdict: GovernanceVerdict
    reason: str = ""
    policy_id: str = ""
    operation: MemoryOperation | None = None
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    # Kernel attestation (v3.5.0) -- unified audit envelope, optional
    envelope: "DecisionEnvelope | None" = None

    def __post_init__(self) -> None:
        # C1: Reject audit_metadata keys that collide with core fields.
        collisions = set(self.audit_metadata) & self._RESERVED_KEYS
        if collisions:
            raise ValueError(
                f"MemoryGovernanceDecision.audit_metadata contains reserved keys: "
                f"{sorted(collisions)}"
            )
        # Freeze mutable audit_metadata to prevent post-construction mutation.
        object.__setattr__(
            self, "audit_metadata", _types.MappingProxyType(dict(self.audit_metadata))
        )

    @property
    def allowed(self) -> bool:
        """True when operation may proceed (ALLOW, QUARANTINE, or DEGRADE)."""
        return self.verdict in (
            GovernanceVerdict.ALLOW,
            GovernanceVerdict.QUARANTINE,
            GovernanceVerdict.DEGRADE,
        )

    @property
    def denied(self) -> bool:
        """True only when verdict is DENY."""
        return self.verdict is GovernanceVerdict.DENY

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict suitable for audit log emission.

        When an envelope is present its fields are merged in under an
        ``envelope_`` prefix so that audit consumers can access both the
        memory-governance verdict and the unified attestation fields from a
        single flat record.
        """
        result: dict[str, Any] = {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "policy_id": self.policy_id,
        }
        if self.operation is not None:
            result["operation_action"] = self.operation.action.value
            result["operation_resource_id"] = self.operation.resource_id
            result["operation_agent_id"] = self.operation.agent_id
            result["operation_namespace"] = self.operation.namespace
            result["operation_provenance"] = self.operation.provenance.value
            result["operation_content_size_bytes"] = self.operation.content_size_bytes
        result.update(self.audit_metadata)
        if self.envelope is not None:
            for key, value in self.envelope.to_audit_dict().items():
                result[f"envelope_{key}"] = value
        return result
