"""A2A trust boundary types for cross-agent identity and policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TrustLevel(str, Enum):
    """Trust classification for agents participating in cross-agent protocols."""

    UNTRUSTED = "untrusted"
    PROVISIONAL = "provisional"
    TRUSTED = "trusted"
    PRIVILEGED = "privileged"


@dataclass(frozen=True)
class AgentIdentity:
    """Identity of an agent in cross-agent communication."""

    agent_id: str
    origin: str  # "local", "a2a", "mcp"
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    metadata: dict[str, Any] = field(default_factory=dict)

    _VALID_ORIGINS: frozenset[str] = field(
        default=frozenset({"local", "a2a", "mcp"}),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.origin not in self._VALID_ORIGINS:
            raise ValueError(
                f"AgentIdentity.origin={self.origin!r} is invalid; "
                f"valid: {sorted(self._VALID_ORIGINS)}"
            )
        if not self.agent_id:
            raise ValueError("AgentIdentity.agent_id must not be empty")


@dataclass(frozen=True)
class TrustPolicy:
    """Configuration for trust escalation behavior."""

    default_trust: TrustLevel = TrustLevel.UNTRUSTED
    promotion_threshold: int = 10
    allow_promotion_to: TrustLevel = TrustLevel.PROVISIONAL

    def __post_init__(self) -> None:
        if self.promotion_threshold <= 0:
            raise ValueError(
                f"TrustPolicy.promotion_threshold must be positive, got {self.promotion_threshold}"
            )
        if self.allow_promotion_to == TrustLevel.PRIVILEGED:
            raise ValueError(
                "TrustPolicy.allow_promotion_to must not be PRIVILEGED "
                "(PRIVILEGED requires explicit manual assignment)"
            )
