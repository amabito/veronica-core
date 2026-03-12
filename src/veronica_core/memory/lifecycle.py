"""Memory provenance lifecycle -- formal state transition rules.

Defines which MemoryProvenance transitions are permitted, which are
forbidden, and the conditions under which transitions occur.

This module is pure and deterministic -- no side effects, no storage,
no I/O.  It expresses kernel-level rules that TriMemory or any other
memory engine must respect.
"""

from __future__ import annotations

__all__ = [
    "ProvenanceLifecycle",
    "TransitionResult",
]

from dataclasses import dataclass
from enum import Enum

from veronica_core.memory.types import (
    TRUST_RANK as _TRUST_RANK,
    MemoryProvenance,
)


class _Reason(str, Enum):
    """Internal reason codes for transition outcomes."""

    OK = "ok"
    FORBIDDEN = "forbidden"
    TRUST_INSUFFICIENT = "trust_insufficient"
    VERIFICATION_REQUIRED = "verification_required"
    ALREADY_IN_STATE = "already_in_state"
    INVALID_SOURCE = "invalid_source"


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a provenance state transition attempt.

    Attributes:
        allowed: Whether the transition was permitted.
        reason: Machine-readable reason code.
        message: Human-readable explanation.
        from_state: Source provenance state.
        to_state: Target provenance state.
    """

    allowed: bool
    reason: str
    message: str
    from_state: MemoryProvenance
    to_state: MemoryProvenance


# ------------------------------------------------------------------
# Transition matrix
# ------------------------------------------------------------------

# Permitted transitions: from_state -> frozenset of valid to_states.
_ALLOWED_TRANSITIONS: dict[MemoryProvenance, frozenset[MemoryProvenance]] = {
    MemoryProvenance.UNKNOWN: frozenset({
        MemoryProvenance.UNVERIFIED,
        MemoryProvenance.QUARANTINED,
    }),
    MemoryProvenance.UNVERIFIED: frozenset({
        MemoryProvenance.VERIFIED,
        MemoryProvenance.QUARANTINED,
    }),
    MemoryProvenance.QUARANTINED: frozenset({
        MemoryProvenance.UNVERIFIED,
        MemoryProvenance.VERIFIED,
    }),
    MemoryProvenance.VERIFIED: frozenset({
        MemoryProvenance.QUARANTINED,
    }),
}

# Minimum trust levels required for each transition.
# Key: (from, to). Value: minimum trust level string.
# Trust ranking: untrusted < provisional < trusted < privileged.
_TRUST_REQUIREMENTS: dict[tuple[MemoryProvenance, MemoryProvenance], str] = {
    # Quarantine entry: any trust level can quarantine.
    (MemoryProvenance.UNKNOWN, MemoryProvenance.QUARANTINED): "untrusted",
    (MemoryProvenance.UNVERIFIED, MemoryProvenance.QUARANTINED): "untrusted",
    (MemoryProvenance.VERIFIED, MemoryProvenance.QUARANTINED): "untrusted",
    # Ingest: initial classification.
    (MemoryProvenance.UNKNOWN, MemoryProvenance.UNVERIFIED): "provisional",
    # Verification promotion: requires trusted+.
    (MemoryProvenance.UNVERIFIED, MemoryProvenance.VERIFIED): "trusted",
    (MemoryProvenance.QUARANTINED, MemoryProvenance.VERIFIED): "privileged",
    # Release from quarantine to unverified: requires trusted.
    (MemoryProvenance.QUARANTINED, MemoryProvenance.UNVERIFIED): "trusted",
}

# ------------------------------------------------------------------
# Degrade provenance tightening rules
# ------------------------------------------------------------------

# When a DEGRADE verdict is applied, provenance is tightened (never loosened).
# Maps current provenance to the tightened provenance under degradation.
_DEGRADE_TIGHTENING: dict[MemoryProvenance, MemoryProvenance] = {
    MemoryProvenance.VERIFIED: MemoryProvenance.UNVERIFIED,
    MemoryProvenance.UNVERIFIED: MemoryProvenance.QUARANTINED,
    MemoryProvenance.QUARANTINED: MemoryProvenance.QUARANTINED,
    MemoryProvenance.UNKNOWN: MemoryProvenance.QUARANTINED,
}


class ProvenanceLifecycle:
    """Formal state machine for MemoryProvenance transitions.

    All methods are pure and deterministic.  No side effects.

    Usage::

        lifecycle = ProvenanceLifecycle()
        result = lifecycle.validate_transition(
            MemoryProvenance.UNVERIFIED,
            MemoryProvenance.VERIFIED,
            trust_level="trusted",
        )
        if not result.allowed:
            raise PermissionError(result.message)
    """

    def validate_transition(
        self,
        from_state: MemoryProvenance,
        to_state: MemoryProvenance,
        trust_level: str = "",
    ) -> TransitionResult:
        """Check whether a provenance transition is permitted.

        Args:
            from_state: Current provenance classification.
            to_state: Desired provenance classification.
            trust_level: Trust level of the agent requesting the transition.
                         One of: "untrusted", "provisional", "trusted", "privileged".
                         Empty string is treated as "untrusted".

        Returns:
            TransitionResult indicating whether the transition is allowed.
        """
        if not isinstance(from_state, MemoryProvenance):
            raise TypeError(
                f"from_state must be MemoryProvenance, got {type(from_state).__name__}"
            )
        if not isinstance(to_state, MemoryProvenance):
            raise TypeError(
                f"to_state must be MemoryProvenance, got {type(to_state).__name__}"
            )

        # Same-state transition is a no-op (allowed but flagged).
        if from_state is to_state:
            return TransitionResult(
                allowed=True,
                reason=_Reason.ALREADY_IN_STATE.value,
                message=f"already in {from_state.value}",
                from_state=from_state,
                to_state=to_state,
            )

        # Check if transition is in the allowed matrix.
        valid_targets = _ALLOWED_TRANSITIONS.get(from_state, frozenset())
        if to_state not in valid_targets:
            return TransitionResult(
                allowed=False,
                reason=_Reason.FORBIDDEN.value,
                message=(
                    f"transition {from_state.value} -> {to_state.value} "
                    f"is not permitted"
                ),
                from_state=from_state,
                to_state=to_state,
            )

        # Check trust level requirement.
        if trust_level is not None and not isinstance(trust_level, str):
            raise TypeError(
                f"trust_level must be a string or None, got {type(trust_level).__name__}"
            )
        trust = trust_level.lower().strip() if trust_level else "untrusted"
        if trust not in _TRUST_RANK:
            # Unknown trust level -> deny (fail-closed).
            return TransitionResult(
                allowed=False,
                reason=_Reason.TRUST_INSUFFICIENT.value,
                message=f"unknown trust level {trust_level!r} (fail-closed)",
                from_state=from_state,
                to_state=to_state,
            )

        required = _TRUST_REQUIREMENTS.get(
            (from_state, to_state), "privileged",
        )
        if _TRUST_RANK[trust] < _TRUST_RANK[required]:
            return TransitionResult(
                allowed=False,
                reason=_Reason.TRUST_INSUFFICIENT.value,
                message=(
                    f"transition {from_state.value} -> {to_state.value} "
                    f"requires {required!r} trust, agent has {trust!r}"
                ),
                from_state=from_state,
                to_state=to_state,
            )

        return TransitionResult(
            allowed=True,
            reason=_Reason.OK.value,
            message=f"transition {from_state.value} -> {to_state.value} permitted",
            from_state=from_state,
            to_state=to_state,
        )

    def degrade_provenance(
        self, current: MemoryProvenance,
    ) -> MemoryProvenance:
        """Return the tightened provenance after a DEGRADE verdict.

        Provenance is never loosened by degradation:
        - VERIFIED -> UNVERIFIED
        - UNVERIFIED -> QUARANTINED
        - QUARANTINED -> QUARANTINED (already tightest non-UNKNOWN)
        - UNKNOWN -> QUARANTINED
        """
        if not isinstance(current, MemoryProvenance):
            raise TypeError(
                f"current must be MemoryProvenance, got {type(current).__name__}"
            )
        return _DEGRADE_TIGHTENING[current]

    def can_promote_to_verified(
        self,
        current: MemoryProvenance,
        trust_level: str = "",
    ) -> bool:
        """Check if content can be promoted to VERIFIED from *current*.

        Convenience method wrapping validate_transition().
        """
        result = self.validate_transition(
            current, MemoryProvenance.VERIFIED, trust_level=trust_level,
        )
        return result.allowed

    def quarantine_entry_conditions(self) -> dict[str, str]:
        """Return human-readable quarantine entry conditions.

        Returns a mapping from source state to the minimum trust level
        required to quarantine content in that state.
        """
        result: dict[str, str] = {}
        for (from_state, to_state), trust in _TRUST_REQUIREMENTS.items():
            if to_state is MemoryProvenance.QUARANTINED:
                result[from_state.value] = trust
        return result

    def all_transitions(self) -> dict[str, list[str]]:
        """Return the complete transition matrix as a plain dict.

        Useful for diagnostics and audit export.
        """
        return {
            state.value: sorted(t.value for t in targets)
            for state, targets in _ALLOWED_TRANSITIONS.items()
        }
