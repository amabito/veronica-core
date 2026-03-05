"""Trust-based policy routing for A2A agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veronica_core.a2a.types import AgentIdentity, TrustLevel

if TYPE_CHECKING:
    from veronica_core.shield.pipeline import ShieldPipeline


class TrustBasedPolicyRouter:
    """Routes agents to ShieldPipelines based on their trust level.

    Each TrustLevel maps to a distinct ShieldPipeline configuration.
    Unmapped trust levels fall back to default_policy.

    The policies dict is read-only after construction (thread-safe
    for concurrent route() calls without locking).

    Args:
        policies: Mapping from TrustLevel to ShieldPipeline.
        default_policy: Fallback pipeline for unmapped trust levels.
            If None, an empty ShieldPipeline is created.
    """

    def __init__(
        self,
        policies: dict[TrustLevel, "ShieldPipeline"] | None = None,
        default_policy: "ShieldPipeline | None" = None,
    ) -> None:
        from veronica_core.shield.pipeline import ShieldPipeline

        self._policies: dict[TrustLevel, ShieldPipeline] = dict(policies or {})
        self._default: ShieldPipeline = default_policy if default_policy is not None else ShieldPipeline()

    def route(self, identity: AgentIdentity) -> "ShieldPipeline":
        """Return the ShieldPipeline for the given agent identity."""
        return self.get_policy_for(identity.trust_level)

    def get_policy_for(self, trust_level: TrustLevel) -> "ShieldPipeline":
        """Return the ShieldPipeline for a specific trust level."""
        return self._policies.get(trust_level, self._default)
