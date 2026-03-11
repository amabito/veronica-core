"""Compactness policy evaluator for memory operations."""
from __future__ import annotations

__all__ = ["CompactnessEvaluator"]

from typing import Any

from veronica_core.memory.types import (
    CompactnessConstraints,
    DegradeDirective,
    GovernanceVerdict,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    ThreatContext,
)

_POLICY_ID = "compactness"


class CompactnessEvaluator:
    """Evaluates memory operations against compactness constraints.

    Implements the MemoryGovernanceHook protocol so it can be registered
    with MemoryGovernor.

    Evaluation order:
    1. No constraints in context AND no defaults -> ALLOW immediately.
    2. max_payload_bytes exceeded -> DENY (hard limit).
    3. max_packet_tokens exceeded -> DEGRADE (summary_required via compact mode).
    4. max_attributes_per_packet exceeded -> DEGRADE.
    5. raw_replay_ratio exceeds max_raw_replay_ratio -> DEGRADE (raw_replay_blocked).
    6. require_compaction_if_over_budget and any limit exceeded -> DEGRADE (summary_required).
    7. prefer_verified_summary and provenance != VERIFIED -> DEGRADE (verified_only).

    When multiple DEGRADE conditions trigger the directives are merged into a
    single DegradeDirective before returning, so callers see one unified
    instruction rather than one-per-condition.

    Thread-safe: no mutable instance state beyond the optional default constraints
    object, which is itself immutable (frozen dataclass).
    """

    def __init__(
        self, default_constraints: CompactnessConstraints | None = None
    ) -> None:
        """Create a CompactnessEvaluator.

        Args:
            default_constraints: Constraints applied when the evaluation context
                carries no ``compactness`` field.  None means "no constraints by
                default" (evaluator behaves as pass-through).
        """
        self._default = default_constraints

    # ------------------------------------------------------------------
    # MemoryGovernanceHook protocol
    # ------------------------------------------------------------------

    def before_op(
        self,
        operation: MemoryOperation,
        context: MemoryPolicyContext | None,
    ) -> MemoryGovernanceDecision:
        """Evaluate compactness constraints for *operation*.

        Returns ALLOW, DEGRADE (with directive), or DENY depending on the
        constraints present in *context* (or the evaluator's defaults).
        """
        constraints = _resolve_constraints(context, self._default)
        if constraints is None:
            return _allow(operation)

        # --- Hard limit: payload bytes ---
        if constraints.max_payload_bytes > 0:
            if operation.content_size_bytes > constraints.max_payload_bytes:
                return MemoryGovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=(
                        f"content_size_bytes {operation.content_size_bytes} "
                        f"exceeds max_payload_bytes {constraints.max_payload_bytes}"
                    ),
                    policy_id=_POLICY_ID,
                    operation=operation,
                    threat_context=ThreatContext(
                        threat_hypothesis="oversized payload exceeds hard limit",
                        mitigation_applied="deny",
                        compactness_enforced=True,
                    ),
                )

        # --- Soft limits: collect DEGRADE flags ---
        degrade_reasons: list[str] = []
        raw_replay_blocked = False
        summary_required = False
        verified_only = False
        max_packet_tokens = 0
        over_any_limit = False

        packet_tokens: int = int(operation.metadata.get("packet_tokens", 0))
        attribute_count: int = int(operation.metadata.get("attribute_count", 0))
        raw_replay_ratio: float = float(
            operation.metadata.get("raw_replay_ratio", 0.0)
        )

        if constraints.max_packet_tokens > 0 and packet_tokens > constraints.max_packet_tokens:
            degrade_reasons.append(
                f"packet_tokens {packet_tokens} > max_packet_tokens "
                f"{constraints.max_packet_tokens}"
            )
            max_packet_tokens = constraints.max_packet_tokens
            summary_required = True
            over_any_limit = True

        if (
            constraints.max_attributes_per_packet > 0
            and attribute_count > constraints.max_attributes_per_packet
        ):
            degrade_reasons.append(
                f"attribute_count {attribute_count} > max_attributes_per_packet "
                f"{constraints.max_attributes_per_packet}"
            )
            over_any_limit = True

        if raw_replay_ratio > constraints.max_raw_replay_ratio:
            degrade_reasons.append(
                f"raw_replay_ratio {raw_replay_ratio:.3f} > max_raw_replay_ratio "
                f"{constraints.max_raw_replay_ratio:.3f}"
            )
            raw_replay_blocked = True
            over_any_limit = True

        if constraints.require_compaction_if_over_budget and over_any_limit:
            summary_required = True
            if "require_compaction_if_over_budget" not in " ".join(degrade_reasons):
                degrade_reasons.append("require_compaction_if_over_budget triggered")

        if constraints.prefer_verified_summary:
            from veronica_core.memory.types import MemoryProvenance
            if operation.provenance is not MemoryProvenance.VERIFIED:
                degrade_reasons.append(
                    f"prefer_verified_summary: provenance is {operation.provenance.value!r}"
                )
                verified_only = True
                over_any_limit = True

        if not degrade_reasons:
            return _allow(operation)

        directive = DegradeDirective(
            mode="compact",
            max_packet_tokens=max_packet_tokens,
            verified_only=verified_only,
            summary_required=summary_required,
            raw_replay_blocked=raw_replay_blocked,
        )
        threat = ThreatContext(
            threat_hypothesis="compactness constraints violated",
            mitigation_applied="degrade",
            degrade_reason="; ".join(degrade_reasons),
            compactness_enforced=True,
        )
        return MemoryGovernanceDecision(
            verdict=GovernanceVerdict.DEGRADE,
            reason="; ".join(degrade_reasons),
            policy_id=_POLICY_ID,
            operation=operation,
            degrade_directive=directive,
            threat_context=threat,
        )

    def after_op(
        self,
        operation: MemoryOperation,
        decision: MemoryGovernanceDecision,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """No-op -- compactness policy has no post-operation side effects."""


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _resolve_constraints(
    context: MemoryPolicyContext | None,
    default: CompactnessConstraints | None,
) -> CompactnessConstraints | None:
    """Return constraints from context, falling back to *default*."""
    if context is not None and context.compactness is not None:
        return context.compactness
    return default


def _allow(operation: MemoryOperation) -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        verdict=GovernanceVerdict.ALLOW,
        reason="no compactness constraints",
        policy_id=_POLICY_ID,
        operation=operation,
    )
