"""Message governance hooks for multi-agent message control.

Provides governance over agent-to-agent messages before they are delivered
or written to memory. This is the entry point for message-level policy --
the message engine itself is NOT implemented here (that's TriMemory's job).
"""
from __future__ import annotations

__all__ = [
    "MessageGovernanceHook",
    "DefaultMessageGovernanceHook",
    "DenyOversizedMessageHook",
    "MessageBridgeHook",
]

import logging
from typing import Any, Protocol, runtime_checkable

from veronica_core.memory.types import (
    BridgePolicy,
    DegradeDirective,
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryProvenance,
    MessageContext,
    ThreatContext,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class MessageGovernanceHook(Protocol):
    """Protocol for message-level governance extension points.

    before_message() evaluates a message before delivery.
    after_message() is fire-and-forget notification.
    """

    def before_message(
        self,
        context: MessageContext,
    ) -> MemoryGovernanceDecision:
        ...

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        ...


class DefaultMessageGovernanceHook:
    """Fail-open: allows all messages."""

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="default allow",
            policy_id="message_default",
        )

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if error is not None:
            logger.warning("[message.hooks] after_message error: %s", error)


class DenyOversizedMessageHook:
    """Denies messages exceeding a byte size limit, degrades near-limit.

    Args:
        max_bytes: Hard limit. Messages above this are DENY.
        degrade_threshold: Fraction of max_bytes. Messages above this but
            below max_bytes get DEGRADE with summary_required=True.
    """

    def __init__(self, max_bytes: int = 1_000_000, degrade_threshold: float = 0.8) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
        if not 0.0 < degrade_threshold <= 1.0:
            raise ValueError(f"degrade_threshold must be in (0, 1], got {degrade_threshold}")
        self._max_bytes = max_bytes
        self._degrade_at = int(max_bytes * degrade_threshold)

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        size = context.content_size_bytes
        if size > self._max_bytes:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"message size {size} exceeds limit {self._max_bytes}",
                policy_id="message_size",
                threat_context=ThreatContext(
                    threat_hypothesis="oversized message may cause resource exhaustion",
                    mitigation_applied="deny",
                    source_trust=context.trust_level,
                ),
            )
        if size > self._degrade_at and size < self._max_bytes:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DEGRADE,
                reason=f"message size {size} exceeds degrade threshold {self._degrade_at}",
                policy_id="message_size",
                degrade_directive=DegradeDirective(
                    mode="compact",
                    summary_required=True,
                    max_content_size_bytes=self._degrade_at,
                ),
                threat_context=ThreatContext(
                    threat_hypothesis="near-limit message may need compaction",
                    mitigation_applied="degrade",
                    compactness_enforced=True,
                    source_trust=context.trust_level,
                ),
            )
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="message within size limits",
            policy_id="message_size",
        )

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        pass


class MessageBridgeHook:
    """Controls message-to-memory promotion using BridgePolicy.

    Evaluates whether an agent message may be written to the memory layer.

    Rules:
    - If not allow_archive -> DENY (no archiving at all)
    - If require_signature and provenance != VERIFIED -> DENY
    - If quarantine_untrusted and trust_level in ("untrusted", "") -> QUARANTINE
    - If message_type not in allowed_types (if configured) -> DENY
    - Otherwise -> ALLOW

    This hook evaluates MessageContext, not MemoryOperation.
    It is intended to be used in a separate message governance pipeline,
    not mixed into MemoryGovernor.
    """

    def __init__(
        self,
        policy: BridgePolicy | None = None,
        allowed_message_types: frozenset[str] | None = None,
    ) -> None:
        self._policy = policy or BridgePolicy()
        self._allowed_types = allowed_message_types

    def before_message(self, context: MessageContext) -> MemoryGovernanceDecision:
        policy = self._policy

        if not policy.allow_archive:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="archive not permitted by bridge policy",
                policy_id="message_bridge",
                threat_context=ThreatContext(
                    threat_hypothesis="uncontrolled message archiving",
                    mitigation_applied="deny",
                ),
            )

        if policy.require_signature and context.provenance != MemoryProvenance.VERIFIED:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"signature required but provenance is {context.provenance.value}",
                policy_id="message_bridge",
                threat_context=ThreatContext(
                    threat_hypothesis="unsigned message promoted to memory",
                    mitigation_applied="deny",
                    source_provenance=context.provenance.value,
                ),
            )

        if self._allowed_types and context.message_type not in self._allowed_types:
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"message type {context.message_type!r} not in allowed types",
                policy_id="message_bridge",
            )

        if policy.quarantine_untrusted and context.trust_level in ("untrusted", ""):
            return MemoryGovernanceDecision(
                verdict=GovernanceVerdict.QUARANTINE,
                reason=f"untrusted message quarantined (trust={context.trust_level!r})",
                policy_id="message_bridge",
                threat_context=ThreatContext(
                    threat_hypothesis="untrusted message injection into memory",
                    mitigation_applied="quarantine",
                    source_trust=context.trust_level or "unknown",
                    source_provenance=context.provenance.value,
                ),
            )

        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="message bridge policy passed",
            policy_id="message_bridge",
        )

    def after_message(
        self,
        context: MessageContext,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        pass
