"""Memory governance readiness diagnostics for VERONICA Core.

Provides a side-effect-free capability snapshot that reports whether
the memory governance stack is properly configured and ready to enforce
policy.

CP consumers can read the snapshot directly without interpreting
internal kernel state.
"""

from __future__ import annotations

__all__ = [
    "MemoryGovernanceReadiness",
    "ReadinessSnapshot",
]

from dataclasses import dataclass
from typing import Any

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.types import ExecutionMode, MemoryView


_AUDIT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ReadinessSnapshot:
    """Immutable snapshot of memory governance readiness state.

    All fields are plain types (str, bool, int, list, dict) for
    direct serialization to JSON.
    """

    # Overall readiness.
    governance_enabled: bool = False
    """True if at least one governance hook is registered."""

    fail_closed: bool = True
    """True if the governor defaults to DENY on no hooks."""

    # Hook inventory.
    hook_count: int = 0
    """Total number of registered hooks."""

    registered_hooks: tuple[str, ...] = ()
    """Class names of registered hooks, in evaluation order."""

    # Specific evaluator presence.
    compactness_evaluator_present: bool = False
    view_policy_evaluator_present: bool = False
    boundary_hook_present: bool = False
    memory_rule_evaluator_present: bool = False

    # Capability report.
    supported_views: tuple[str, ...] = ()
    """All MemoryView values the kernel supports."""

    supported_modes: tuple[str, ...] = ()
    """All ExecutionMode values the kernel supports."""

    degrade_support: bool = False
    """True if any hook can return DEGRADE (presence of known degrade-capable hooks)."""

    lifecycle_support: bool = False
    """True if ProvenanceLifecycle is importable."""

    audit_schema_version: str = _AUDIT_SCHEMA_VERSION
    """Version of the to_audit_dict() output schema."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON or health endpoints."""
        return {
            "governance_enabled": self.governance_enabled,
            "fail_closed": self.fail_closed,
            "hook_count": self.hook_count,
            "registered_hooks": list(self.registered_hooks),
            "compactness_evaluator_present": self.compactness_evaluator_present,
            "view_policy_evaluator_present": self.view_policy_evaluator_present,
            "boundary_hook_present": self.boundary_hook_present,
            "memory_rule_evaluator_present": self.memory_rule_evaluator_present,
            "supported_views": list(self.supported_views),
            "supported_modes": list(self.supported_modes),
            "degrade_support": self.degrade_support,
            "lifecycle_support": self.lifecycle_support,
            "audit_schema_version": self.audit_schema_version,
        }


# Known hook class names for evaluator detection.
_COMPACTNESS_NAMES = frozenset({"CompactnessEvaluator"})
_VIEW_POLICY_NAMES = frozenset({"ViewPolicyEvaluator"})
_BOUNDARY_NAMES = frozenset({"MemoryBoundaryHook"})
_RULE_EVALUATOR_NAMES = frozenset({"MemoryRuleEvaluator"})
_DEGRADE_CAPABLE_NAMES = frozenset(
    {
        "CompactnessEvaluator",
        "ViewPolicyEvaluator",
        "MemoryRuleEvaluator",
    }
)

# Pre-computed enum value tuples (immutable, computed once).
_SUPPORTED_VIEWS: tuple[str, ...] = tuple(sorted(v.value for v in MemoryView))
_SUPPORTED_MODES: tuple[str, ...] = tuple(sorted(m.value for m in ExecutionMode))


class MemoryGovernanceReadiness:
    """Inspects a MemoryGovernor and produces a ReadinessSnapshot.

    Side-effect free: does not modify the governor or any hooks.

    Usage::

        readiness = MemoryGovernanceReadiness()
        snapshot = readiness.check(governor)
        print(snapshot.to_dict())
    """

    def check(self, governor: MemoryGovernor | None) -> ReadinessSnapshot:
        """Produce a ReadinessSnapshot for the given governor.

        If governor is None, returns a snapshot with governance_enabled=False.
        """
        if governor is None:
            return ReadinessSnapshot(
                governance_enabled=False,
                fail_closed=True,
                supported_views=self._supported_views(),
                supported_modes=self._supported_modes(),
                lifecycle_support=self._lifecycle_available(),
            )

        hooks = self._extract_hooks(governor)
        hook_names = tuple(type(h).__name__ for h in hooks)
        name_set = set(hook_names)

        has_compactness = bool(name_set & _COMPACTNESS_NAMES)
        has_view_policy = bool(name_set & _VIEW_POLICY_NAMES)
        has_boundary = bool(name_set & _BOUNDARY_NAMES)
        has_rule_eval = bool(name_set & _RULE_EVALUATOR_NAMES)
        has_degrade = bool(name_set & _DEGRADE_CAPABLE_NAMES)

        return ReadinessSnapshot(
            governance_enabled=len(hooks) > 0,
            fail_closed=governor.fail_closed,
            hook_count=governor.hook_count,
            registered_hooks=hook_names,
            compactness_evaluator_present=has_compactness,
            view_policy_evaluator_present=has_view_policy,
            boundary_hook_present=has_boundary,
            memory_rule_evaluator_present=has_rule_eval,
            supported_views=self._supported_views(),
            supported_modes=self._supported_modes(),
            degrade_support=has_degrade,
            lifecycle_support=self._lifecycle_available(),
            audit_schema_version=_AUDIT_SCHEMA_VERSION,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_hooks(governor: MemoryGovernor) -> list[Any]:
        """Extract the hook list from a governor (read-only snapshot)."""
        # Use the public get_hooks() method for thread-safe hook access.
        return governor.get_hooks()

    @staticmethod
    def _supported_views() -> tuple[str, ...]:
        """Return all MemoryView enum values."""
        return _SUPPORTED_VIEWS

    @staticmethod
    def _supported_modes() -> tuple[str, ...]:
        """Return all ExecutionMode enum values."""
        return _SUPPORTED_MODES

    @staticmethod
    def _lifecycle_available() -> bool:
        """Check if ProvenanceLifecycle is importable."""
        try:
            from veronica_core.memory.lifecycle import ProvenanceLifecycle  # noqa: F401

            return True
        except ImportError:
            return False
