"""Capability model for VERONICA Security Containment Layer."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Capability(enum.Enum):
    """Capabilities that an agent can be granted."""

    READ_REPO = "read_repo"
    EDIT_REPO = "edit_repo"
    BUILD = "build"
    TEST = "test"
    NET_FETCH_ALLOWLIST = "net_fetch_allowlist"
    GIT_PUSH_APPROVAL = "git_push_approval"
    SHELL_BASIC = "shell_basic"
    FILE_READ_SENSITIVE = "file_read_sensitive"


@dataclass(frozen=True)
class CapabilitySet:
    """An immutable set of capabilities granted to an agent."""

    caps: frozenset[Capability] = field(default_factory=frozenset)

    @classmethod
    def dev(cls) -> "CapabilitySet":
        """Developer profile: full local development capabilities."""
        return cls(
            caps=frozenset(
                {
                    Capability.READ_REPO,
                    Capability.EDIT_REPO,
                    Capability.BUILD,
                    Capability.TEST,
                    Capability.SHELL_BASIC,
                }
            )
        )

    @classmethod
    def ci(cls) -> "CapabilitySet":
        """CI profile: read-only plus build and test, no editing or network."""
        return cls(
            caps=frozenset(
                {
                    Capability.READ_REPO,
                    Capability.BUILD,
                    Capability.TEST,
                }
            )
        )

    @classmethod
    def audit(cls) -> "CapabilitySet":
        """Audit profile: read-only access."""
        return cls(
            caps=frozenset(
                {
                    Capability.READ_REPO,
                }
            )
        )


def has_cap(caps: CapabilitySet, capability: Capability) -> bool:
    """Return True if the given capability is present in caps."""
    return capability in caps.caps
