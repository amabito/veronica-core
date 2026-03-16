"""Runtime side-effect classification for execution containment.

Classifies actions by their side-effect profile so that policy rules
can gate decisions based on what an action DOES, not just what it IS.

A shell command that reads a file is different from one that deletes it.
An HTTP GET is different from a POST that mutates external state.
This module provides the vocabulary for those distinctions.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


class SideEffectClass(str, enum.Enum):
    """Classification of action side effects.

    Ordered by increasing severity. Compare with .severity property.
    """

    NONE = "none"
    INFORMATIONAL = "informational"
    READ_LOCAL = "read_local"
    WRITE_LOCAL = "write_local"
    SHELL_EXECUTE = "shell_execute"
    OUTBOUND_NETWORK = "outbound_network"
    EXTERNAL_MUTATION = "external_mutation"
    CREDENTIAL_ACCESS = "credential_access"
    CROSS_AGENT = "cross_agent"
    IRREVERSIBLE = "irreversible"


# Numeric severity for comparison. Higher = more dangerous.
SIDE_EFFECT_SEVERITY: MappingProxyType[str, int] = MappingProxyType(
    {
        SideEffectClass.NONE.value: 0,
        SideEffectClass.INFORMATIONAL.value: 1,
        SideEffectClass.READ_LOCAL.value: 2,
        SideEffectClass.WRITE_LOCAL.value: 4,
        SideEffectClass.SHELL_EXECUTE.value: 6,
        SideEffectClass.OUTBOUND_NETWORK.value: 6,
        SideEffectClass.EXTERNAL_MUTATION.value: 8,
        SideEffectClass.CREDENTIAL_ACCESS.value: 8,
        SideEffectClass.CROSS_AGENT.value: 5,
        SideEffectClass.IRREVERSIBLE.value: 10,
    }
)


def side_effect_severity(effect: str) -> int:
    """Return numeric severity. Unknown effects get max severity (fail-closed)."""
    return SIDE_EFFECT_SEVERITY.get(effect, 10)


@dataclass(frozen=True)
class SideEffectProfile:
    """Immutable profile of an action's side effects.

    An action can have multiple side effects (e.g., a tool that reads a file
    AND makes a network call). The profile carries all of them.
    """

    effects: frozenset[SideEffectClass] = field(default_factory=frozenset)
    description: str = ""
    strict_mode: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from veronica_core._utils import freeze_mapping

        freeze_mapping(self, "metadata")
        # Coerce set -> frozenset for JSON round-trip safety.
        if isinstance(self.effects, set):
            object.__setattr__(self, "effects", frozenset(self.effects))

    @property
    def max_severity(self) -> int:
        """Highest severity among all effects. 0 if no effects."""
        if not self.effects:
            return 0
        return max(side_effect_severity(e.value) for e in self.effects)

    @property
    def has_write(self) -> bool:
        """True if any effect modifies local state."""
        return bool(
            self.effects
            & {
                SideEffectClass.WRITE_LOCAL,
                SideEffectClass.SHELL_EXECUTE,
            }
        )

    @property
    def has_external(self) -> bool:
        """True if any effect reaches outside the process boundary."""
        return bool(
            self.effects
            & {
                SideEffectClass.OUTBOUND_NETWORK,
                SideEffectClass.EXTERNAL_MUTATION,
                SideEffectClass.CROSS_AGENT,
            }
        )

    @property
    def has_dangerous(self) -> bool:
        """True if any effect is high-severity (>=6)."""
        return self.max_severity >= 6

    @property
    def is_read_only(self) -> bool:
        """True if all effects are read-only or informational."""
        return (
            all(side_effect_severity(e.value) <= 2 for e in self.effects)
            if self.effects
            else True
        )

    @property
    def audit_summary(self) -> str:
        """Compact string for audit logs."""
        if not self.effects:
            return "none"
        return ",".join(sorted(e.value for e in self.effects))


# Default classifiers for known action types.
# Maps action literal -> default SideEffectProfile.
ACTION_SIDE_EFFECTS: MappingProxyType[str, SideEffectProfile] = MappingProxyType(
    {
        "file_read": SideEffectProfile(
            effects=frozenset({SideEffectClass.READ_LOCAL}),
            description="local file read",
        ),
        "file_write": SideEffectProfile(
            effects=frozenset({SideEffectClass.WRITE_LOCAL}),
            description="local file write",
        ),
        "shell": SideEffectProfile(
            effects=frozenset({SideEffectClass.SHELL_EXECUTE}),
            description="shell command execution",
        ),
        "net_request": SideEffectProfile(
            effects=frozenset({SideEffectClass.OUTBOUND_NETWORK}),
            description="outbound network request",
        ),
        "browser_navigate": SideEffectProfile(
            effects=frozenset(
                {SideEffectClass.OUTBOUND_NETWORK, SideEffectClass.READ_LOCAL}
            ),
            description="browser navigation",
        ),
        "git_push": SideEffectProfile(
            effects=frozenset(
                {SideEffectClass.EXTERNAL_MUTATION, SideEffectClass.OUTBOUND_NETWORK}
            ),
            description="git push to remote",
        ),
        "git_commit": SideEffectProfile(
            effects=frozenset({SideEffectClass.WRITE_LOCAL}),
            description="local git commit",
        ),
    }
)


def classify_action(
    action: str, metadata: dict[str, Any] | None = None
) -> SideEffectProfile:
    """Classify an action by its side-effect profile.

    Uses ACTION_SIDE_EFFECTS for known actions. Unknown actions get
    a high-severity profile in strict mode (fail-closed).

    Args:
        action: Action literal (shell, file_read, etc.)
        metadata: Optional extra context for classification.

    Returns:
        SideEffectProfile for the action.
    """
    profile = ACTION_SIDE_EFFECTS.get(action)
    if profile is not None:
        return profile
    # Unknown action: fail-closed with max severity.
    return SideEffectProfile(
        effects=frozenset(),
        description=f"unknown action: {action}",
        strict_mode=True,
    )


# Convenience constants
READ_ONLY_PROFILE: SideEffectProfile = SideEffectProfile(
    effects=frozenset({SideEffectClass.READ_LOCAL}),
)
NO_EFFECT_PROFILE: SideEffectProfile = SideEffectProfile(effects=frozenset())


__all__ = [
    "SideEffectClass",
    "SideEffectProfile",
    "SIDE_EFFECT_SEVERITY",
    "ACTION_SIDE_EFFECTS",
    "classify_action",
    "side_effect_severity",
    "READ_ONLY_PROFILE",
    "NO_EFFECT_PROFILE",
]
