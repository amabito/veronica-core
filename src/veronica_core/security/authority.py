"""Authority model for execution containment.

Represents who or what originated a runtime action, enabling policy
rules to distinguish developer-set policy from tool output, retrieved
content, or agent-generated instructions.

Reuses the trust-level vocabulary from veronica_core.memory.types
(untrusted/provisional/trusted/privileged) so that authority checks
and memory governance checks use a single coherent trust model.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


class AuthoritySource(str, enum.Enum):
    """Origin of a runtime action request.

    Each source carries an implicit trust ceiling -- see AUTHORITY_TRUST_CEILING.
    """

    DEVELOPER_POLICY = "developer_policy"
    SYSTEM_CONFIG = "system_config"
    USER_INPUT = "user_input"
    TOOL_OUTPUT = "tool_output"
    RETRIEVED_CONTENT = "retrieved_content"
    MEMORY_CONTENT = "memory_content"
    AGENT_GENERATED = "agent_generated"
    EXTERNAL_MESSAGE = "external_message"
    APPROVED_OVERRIDE = "approved_override"
    UNKNOWN = "unknown"


# Maximum trust level each source can claim. A tool_output cannot
# unilaterally become "privileged" -- it needs explicit approval.
AUTHORITY_TRUST_CEILING: MappingProxyType[str, str] = MappingProxyType(
    {
        AuthoritySource.DEVELOPER_POLICY.value: "privileged",
        AuthoritySource.SYSTEM_CONFIG.value: "privileged",
        AuthoritySource.USER_INPUT.value: "trusted",
        AuthoritySource.APPROVED_OVERRIDE.value: "trusted",
        AuthoritySource.TOOL_OUTPUT.value: "provisional",
        AuthoritySource.RETRIEVED_CONTENT.value: "provisional",
        AuthoritySource.MEMORY_CONTENT.value: "provisional",
        AuthoritySource.AGENT_GENERATED.value: "provisional",
        AuthoritySource.EXTERNAL_MESSAGE.value: "untrusted",
        AuthoritySource.UNKNOWN.value: "untrusted",
    }
)


@dataclass(frozen=True)
class AuthorityClaim:
    """Immutable claim about who originated an action.

    Flows through ExecutionContext -> PolicyContext -> PolicyDecision -> AuditLog.

    The effective_trust_level is the MINIMUM of:
    - The trust ceiling for this source type
    - Any explicitly asserted trust level
    This prevents privilege escalation: tool_output cannot claim "privileged".
    """

    source: AuthoritySource = AuthoritySource.UNKNOWN
    asserted_trust: str = ""  # Caller's claim. Capped by source ceiling.
    parent_source: AuthoritySource | None = None  # Who spawned this action
    chain: tuple[str, ...] = ()  # Ancestry: ("user-abc", "agent-1", "tool-search")
    approval_id: str | None = None  # If approved_override, which approval
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        from veronica_core._utils import freeze_mapping

        freeze_mapping(self, "metadata")
        # Coerce list -> tuple for JSON round-trip safety.
        if isinstance(self.chain, list):
            object.__setattr__(self, "chain", tuple(self.chain))

    @property
    def effective_trust_level(self) -> str:
        """Trust level capped by source ceiling. Fail-closed: unknown -> untrusted."""
        from veronica_core.memory.types import trust_rank

        ceiling = AUTHORITY_TRUST_CEILING.get(self.source.value, "untrusted")
        if not self.asserted_trust:
            return ceiling
        ceiling_rank = trust_rank(ceiling)
        asserted_rank = trust_rank(self.asserted_trust)
        # Take the lower of ceiling and asserted.
        if asserted_rank <= ceiling_rank:
            return self.asserted_trust
        return ceiling

    @property
    def trust_rank(self) -> int:
        """Numeric rank for comparison. 0=untrusted, 3=privileged."""
        from veronica_core.memory.types import trust_rank

        return trust_rank(self.effective_trust_level)

    def derives(self, child_source: AuthoritySource, **kwargs: Any) -> "AuthorityClaim":
        """Create a derived claim. Authority cannot escalate in derivation."""
        new_chain = (*self.chain, self.source.value)
        return AuthorityClaim(
            source=child_source,
            asserted_trust=self.effective_trust_level,  # inherit, don't escalate
            parent_source=self.source,
            chain=new_chain,
            **kwargs,
        )

    def with_approval(self, approval_id: str) -> "AuthorityClaim":
        """Elevate to APPROVED_OVERRIDE with audit trail."""
        return AuthorityClaim(
            source=AuthoritySource.APPROVED_OVERRIDE,
            asserted_trust="trusted",  # approval grants up to trusted
            parent_source=self.source,
            chain=(*self.chain, self.source.value),
            approval_id=approval_id,
        )

    @property
    def denied_reason(self) -> str:
        """Reason string for audit when authority is insufficient."""
        return (
            f"authority insufficient: source={self.source.value}, "
            f"effective_trust={self.effective_trust_level}, "
            f"chain={self.chain}"
        )


# Default: fail-closed. Unknown origin = untrusted.
UNKNOWN_AUTHORITY: AuthorityClaim = AuthorityClaim(source=AuthoritySource.UNKNOWN)


def is_low_authority(authority: object) -> bool:
    """Return True if *authority* is below 'trusted' rank.

    Low-authority sources (tool_output, retrieved_content, memory_content,
    agent_generated, external_message, unknown) should not bypass approval
    gates.  Returns False for non-AuthorityClaim objects (backward compat).
    Fail-closed: returns False on unexpected errors (non-claim treated as
    not-low, which is the pre-existing behavior from the static methods).
    """
    try:
        if not isinstance(authority, AuthorityClaim):
            return False
        from veronica_core.memory.types import trust_rank

        return trust_rank(authority.effective_trust_level) < trust_rank("trusted")
    except Exception:
        return False


def is_policy_authority(authority: object) -> bool:
    """Return True only for developer_policy or system_config sources.

    These two authority sources represent deliberate operator intent and
    may override sandbox restrictions.  Returns False for non-AuthorityClaim
    objects (backward compat).  Fail-closed: returns False on unexpected errors.
    """
    try:
        if not isinstance(authority, AuthorityClaim):
            return False
        return authority.source in (
            AuthoritySource.DEVELOPER_POLICY,
            AuthoritySource.SYSTEM_CONFIG,
        )
    except Exception:
        return False


__all__ = [
    "AuthoritySource",
    "AuthorityClaim",
    "AUTHORITY_TRUST_CEILING",
    "UNKNOWN_AUTHORITY",
    "is_low_authority",
    "is_policy_authority",
]
