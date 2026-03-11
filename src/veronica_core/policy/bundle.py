"""Policy-attested bundle types for VERONICA Core (v3.2+).

Provides immutable, content-hashed policy bundles that can be
cryptographically signed and distributed to execution agents.

These types are the foundation for policy attestation -- they hold the
canonical definition of what rules apply, who issued them, and whether
the content has been tampered with.

No external dependencies are required.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from veronica_core._utils import freeze_mapping


@dataclass(frozen=True)
class PolicyMetadata:
    """Immutable metadata for a policy bundle.

    Attributes:
        policy_id: Non-empty unique identifier for this policy.
        version: Human-readable version string (e.g. "1.0.0").
        epoch: Non-negative monotonic counter for invalidation ordering.
                Higher epoch supersedes lower epoch bundles.
        created_at: Unix timestamp (seconds) when the bundle was created.
        issuer: Identity of the system or operator that created the bundle.
        description: Human-readable description of the policy's purpose.
        tags: Arbitrary key-value metadata for routing or filtering.
        content_hash: SHA-256 hex digest of the canonical rules JSON.
                      Empty string means the hash has not been set yet.
    """

    policy_id: str
    version: str = "1.0.0"
    epoch: int = 0
    created_at: float = field(default_factory=time.time)
    issuer: str = ""
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.policy_id or not isinstance(self.policy_id, str):
            raise ValueError("PolicyMetadata.policy_id must be a non-empty string")
        if not isinstance(self.epoch, int) or self.epoch < 0:
            raise ValueError(
                f"PolicyMetadata.epoch must be a non-negative integer, got {self.epoch!r}"
            )
        # Freeze mutable tags to prevent post-construction mutation.
        freeze_mapping(self, "tags")


@dataclass(frozen=True)
class PolicyRule:
    """Immutable single rule within a policy bundle.

    Attributes:
        rule_id: Non-empty unique identifier for this rule within the bundle.
        rule_type: Rule category (e.g. "budget", "step", "retry", "shell",
                   "memory", "network", "file", "git", "trust", "custom").
        parameters: Type-specific configuration dict forwarded to the rule engine.
        enabled: Whether this rule participates in evaluation. Default True.
        priority: Execution order -- lower value runs earlier. Default 100.
    """

    rule_id: str
    rule_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise ValueError("PolicyRule.rule_id must be a non-empty string")
        if not self.rule_type or not isinstance(self.rule_type, str):
            raise ValueError("PolicyRule.rule_type must be a non-empty string")
        # Freeze mutable parameters to prevent post-construction tampering
        # that would invalidate content_hash.
        freeze_mapping(self, "parameters")


def _canonical_rules_json(rules: tuple[PolicyRule, ...]) -> str:
    """Return a deterministic JSON string for *rules*.

    Rules are sorted by rule_id before serialization to ensure
    that insertion order does not affect the content hash.
    """
    sorted_rules = sorted(rules, key=lambda r: r.rule_id)
    serializable = [
        {
            "rule_id": r.rule_id,
            "rule_type": r.rule_type,
            "parameters": dict(r.parameters),
            "enabled": r.enabled,
            "priority": r.priority,
        }
        for r in sorted_rules
    ]
    return json.dumps(serializable, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PolicyBundle:
    """Immutable, content-hashed collection of policy rules.

    A PolicyBundle combines metadata describing who issued the policy,
    a tuple of rules that define the policy's behaviour, and an optional
    HMAC/signature for tamper detection.

    Attributes:
        metadata: Identity and provenance metadata.
        rules: Immutable ordered tuple of all rules (enabled and disabled).
        signature: Hex-encoded signature of the bundle content.
                   Empty string means the bundle is unsigned.
    """

    metadata: PolicyMetadata
    rules: tuple[PolicyRule, ...] = field(default_factory=tuple)
    signature: str = ""

    # Cached computed values (not part of frozen identity).
    _cached_content_hash: str = field(default="", init=False, repr=False, compare=False)
    _cached_active_rules: tuple[PolicyRule, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # Coerce rules to tuple to prevent post-construction mutation via list.
        if not isinstance(self.rules, tuple):
            object.__setattr__(self, "rules", tuple(self.rules))

    # ------------------------------------------------------------------
    # Content hashing
    # ------------------------------------------------------------------

    def content_hash(self) -> str:
        """Return the SHA-256 hex digest of the canonical rules JSON.

        The hash covers rule_id, rule_type, parameters, enabled, and priority
        for every rule, sorted by rule_id for determinism.  The result is
        cached after first computation.
        """
        if self._cached_content_hash:
            return self._cached_content_hash
        canonical = _canonical_rules_json(self.rules)
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        object.__setattr__(self, "_cached_content_hash", h)
        return h

    def verify_content_hash(self) -> bool:
        """Return True if metadata.content_hash matches the computed hash.

        Returns False if metadata.content_hash is empty (not set).
        """
        stored = self.metadata.content_hash
        if not stored:
            return False
        return hmac.compare_digest(stored, self.content_hash())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_signed(self) -> bool:
        """Return True if a non-empty signature is present."""
        return bool(self.signature)

    @property
    def active_rules(self) -> tuple[PolicyRule, ...]:
        """Return enabled rules sorted by priority (ascending -- lower runs first).

        The result is cached after first computation.
        """
        if self._cached_active_rules is not None:
            return self._cached_active_rules
        result = tuple(
            sorted(
                (r for r in self.rules if r.enabled),
                key=lambda r: (r.priority, r.rule_id),
            )
        )
        object.__setattr__(self, "_cached_active_rules", result)
        return result
