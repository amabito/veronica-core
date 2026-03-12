"""Memory Governance ABI -- core types for governing memory operations.

This module defines the type vocabulary for memory governance:
- MemoryAction: what kind of operation is requested
- MemoryProvenance: trust level of stored content
- MemoryOperation: fully-described memory operation request
- MemoryPolicyContext: ambient context for policy evaluation
- GovernanceVerdict: categorical outcome of a governance decision
- MemoryGovernanceDecision: full decision record with audit trail
- MemoryView: memory namespace view classification (v3.6.0)
- ExecutionMode: runtime mode for scoped access policy (v3.6.0)
- DegradeDirective: structured DEGRADE parameters (v3.6.0)
- CompactnessConstraints: packet size and content policy (v3.6.0)
- MessageContext: message governance context (v3.6.0)
- BridgePolicy: message-to-memory promotion rules (v3.6.0)
- ThreatContext: threat-aware audit metadata (v3.6.0)

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
    "MemoryView",
    "ExecutionMode",
    "DegradeDirective",
    "CompactnessConstraints",
    "MessageContext",
    "BridgePolicy",
    "ThreatContext",
    "TRUST_RANK",
    "trust_rank",
]

import time
import types as _types
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from veronica_core._utils import freeze_mapping

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


class MemoryView(str, Enum):
    """Memory namespace view classification.

    Controls which memory regions an operation can access.
    Policy evaluators combine view with trust_level and execution_mode
    to determine access.
    """

    AGENT_PRIVATE = "agent_private"
    LOCAL_WORKING = "local_working"
    TEAM_SHARED = "team_shared"
    SESSION_STATE = "session_state"
    VERIFIED_ARCHIVE = "verified_archive"
    PROVISIONAL_ARCHIVE = "provisional_archive"
    QUARANTINED = "quarantined"


class ExecutionMode(str, Enum):
    """Runtime execution mode for scoped memory access policy.

    Each mode defines a different access posture:
    - LIVE: production execution, most restrictive by default
    - REPLAY: broader read, write denied
    - SIMULATION: no privileged promotion
    - CONSOLIDATION: copy-on-write only
    - AUDIT_REVIEW: quarantined read for auditor role
    """

    LIVE = "live_execution"
    REPLAY = "replay"
    SIMULATION = "simulation"
    CONSOLIDATION = "consolidation"
    AUDIT_REVIEW = "audit_review"


# Trust levels in ascending privilege order.
# Shared across view_policy, lifecycle, and memory_rules.
# Frozen via MappingProxyType to prevent external mutation.
TRUST_RANK: _types.MappingProxyType[str, int] = _types.MappingProxyType({
    "untrusted": 0,
    "provisional": 1,
    "trusted": 2,
    "privileged": 3,
})


def trust_rank(trust_level: str) -> int:
    """Return numeric rank for *trust_level* (0 for unknown)."""
    return TRUST_RANK.get(trust_level.lower(), 0)


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
        freeze_mapping(self, "metadata")


@dataclass(frozen=True)
class DegradeDirective:
    """Structured parameters for a DEGRADE verdict.

    When a governance hook returns DEGRADE, it can attach a DegradeDirective
    that specifies HOW the degradation should be applied. The caller (TriMemory
    bridge or equivalent) reads these fields to transform the operation.

    All fields are optional -- omitted fields mean "no constraint on this axis".
    """

    mode: str = ""
    """Degradation mode hint: 'compact', 'redact', 'truncate', 'downscope'."""

    max_packet_tokens: int = 0
    """Maximum token count for the output packet. 0 = no limit."""

    allowed_provenance: tuple[str, ...] = ()
    """Only content with these provenance values may pass. Empty = all."""

    verified_only: bool = False
    """If True, only verified-provenance content may pass."""

    summary_required: bool = False
    """If True, raw content must be summarized before delivery."""

    raw_replay_blocked: bool = False
    """If True, raw replay of memory content is denied; compact form required."""

    namespace_downscoped_to: str = ""
    """Restrict operation to this namespace instead of the requested one."""

    redacted_fields: tuple[str, ...] = ()
    """Field names to redact from the content before delivery."""

    max_content_size_bytes: int = 0
    """Maximum byte size for the content payload. 0 = no limit."""


@dataclass(frozen=True)
class CompactnessConstraints:
    """Policy parameters governing memory packet size and shape.

    These constraints tell the TriMemory bridge how much content can be
    delivered and in what form. The governance layer evaluates these against
    the operation; the bridge enforces the actual compaction.
    """

    max_packet_tokens: int = 0
    """Maximum tokens in a single response packet. 0 = no limit."""

    max_raw_replay_ratio: float = 1.0
    """Fraction of raw content allowed vs compact form. 1.0 = all raw OK."""

    require_compaction_if_over_budget: bool = False
    """Force compaction when packet would exceed token budget."""

    prefer_verified_summary: bool = False
    """Prefer verified summary over raw unverified content."""

    max_attributes_per_packet: int = 0
    """Maximum attribute count in a packet. 0 = no limit."""

    max_payload_bytes: int = 0
    """Maximum byte size for the entire payload. 0 = no limit."""


@dataclass(frozen=True)
class MessageContext:
    """Context for message governance evaluation.

    Describes an agent-to-agent or tool-to-agent message that is subject
    to governance before delivery or memory write.
    """

    sender_id: str = ""
    recipient_id: str = ""
    message_type: str = ""
    """Message category: 'agent_to_agent', 'tool_result', 'user_input', etc."""

    content_size_bytes: int = 0
    trust_level: str = ""
    provenance: MemoryProvenance = MemoryProvenance.UNKNOWN
    namespace: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.content_size_bytes < 0:
            raise ValueError(
                f"MessageContext.content_size_bytes must be >= 0, "
                f"got {self.content_size_bytes}"
            )
        freeze_mapping(self, "metadata")


@dataclass(frozen=True)
class BridgePolicy:
    """Rules for message-to-memory promotion.

    Controls whether and how an agent message can be written into
    the memory layer (archive, session state, etc.).
    """

    allow_archive: bool = False
    """Whether the message may be archived at all."""

    require_signature: bool = False
    """Require cryptographic signature for archive eligibility."""

    max_promotion_level: str = "provisional"
    """Highest memory view the message can promote to.
    One of: 'provisional_archive', 'verified_archive', 'session_state'."""

    quarantine_untrusted: bool = True
    """Route untrusted messages to quarantined view."""

    write_once_scratch: bool = True
    """Allow write-once temporary scratch area for messages."""


@dataclass(frozen=True)
class ThreatContext:
    """Threat-model-aware audit metadata.

    Attached to governance decisions so post-hoc audit can reconstruct
    why a decision was made and what threat assumptions applied.
    """

    threat_hypothesis: str = ""
    """What threat this decision guards against."""

    mitigation_applied: str = ""
    """What mitigation was applied (e.g., 'redact', 'downscope', 'deny')."""

    degrade_reason: str = ""
    """Why degradation was chosen over full allow or deny."""

    degraded_fields: tuple[str, ...] = ()
    """Which fields were degraded or redacted."""

    effective_scope: str = ""
    """The scope that was actually applied (may differ from requested)."""

    effective_view: str = ""
    """The memory view that was actually used."""

    compactness_enforced: bool = False
    """Whether compactness constraints were applied."""

    source_trust: str = ""
    """Trust level of the source agent at decision time."""

    source_provenance: str = ""
    """Provenance classification of the source content."""


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
    # v3.6.0: scoped execution context
    memory_view: MemoryView = MemoryView.LOCAL_WORKING
    execution_mode: ExecutionMode = ExecutionMode.LIVE
    source_role: str = ""
    compactness: CompactnessConstraints | None = None


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
    # v3.6.0: structured DEGRADE parameters
    degrade_directive: DegradeDirective | None = None
    # v3.6.0: threat-model audit context
    threat_context: ThreatContext | None = None

    def __post_init__(self) -> None:
        # C1: Reject audit_metadata keys that collide with core fields.
        collisions = set(self.audit_metadata) & self._RESERVED_KEYS
        if collisions:
            raise ValueError(
                f"MemoryGovernanceDecision.audit_metadata contains reserved keys: "
                f"{sorted(collisions)}"
            )
        # Freeze mutable audit_metadata to prevent post-construction mutation.
        freeze_mapping(self, "audit_metadata")

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
        if self.degrade_directive is not None:
            d = self.degrade_directive
            result["degrade_mode"] = d.mode
            result["degrade_max_packet_tokens"] = d.max_packet_tokens
            result["degrade_verified_only"] = d.verified_only
            result["degrade_summary_required"] = d.summary_required
            result["degrade_raw_replay_blocked"] = d.raw_replay_blocked
            if d.namespace_downscoped_to:
                result["degrade_namespace_downscoped_to"] = d.namespace_downscoped_to
            if d.redacted_fields:
                result["degrade_redacted_fields"] = list(d.redacted_fields)
        if self.threat_context is not None:
            t = self.threat_context
            if t.threat_hypothesis:
                result["threat_hypothesis"] = t.threat_hypothesis
            if t.mitigation_applied:
                result["threat_mitigation_applied"] = t.mitigation_applied
            if t.degrade_reason:
                result["threat_degrade_reason"] = t.degrade_reason
            if t.degraded_fields:
                result["threat_degraded_fields"] = list(t.degraded_fields)
            if t.effective_scope:
                result["threat_effective_scope"] = t.effective_scope
            if t.effective_view:
                result["threat_effective_view"] = t.effective_view
            if t.compactness_enforced:
                result["threat_compactness_enforced"] = True
            if t.source_trust:
                result["threat_source_trust"] = t.source_trust
            if t.source_provenance:
                result["threat_source_provenance"] = t.source_provenance
        return result
