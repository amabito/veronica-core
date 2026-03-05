"""A2A (Agent-to-Agent) trust boundary for cross-agent communication.

This module implements the trust boundary layer for agents communicating
via the A2A protocol. Agents are classified into trust levels
(UNTRUSTED, PROVISIONAL, TRUSTED, PRIVILEGED) and subject to policy-based
routing and escalation tracking.

Usage::

    from veronica_core.a2a import (
        TrustLevel,
        AgentIdentity,
        TrustPolicy,
        TrustBasedPolicyRouter,
        TrustEscalationTracker,
    )

    policy = TrustPolicy(promotion_threshold=5)
    tracker = TrustEscalationTracker(policy=policy)
    router = TrustBasedPolicyRouter()
"""

from veronica_core.a2a.card import identity_from_a2a_card
from veronica_core.a2a.escalation import TrustEscalationTracker
from veronica_core.a2a.router import TrustBasedPolicyRouter
from veronica_core.a2a.types import AgentIdentity, TrustLevel, TrustPolicy

__all__ = [
    "TrustLevel",
    "AgentIdentity",
    "TrustPolicy",
    "TrustBasedPolicyRouter",
    "TrustEscalationTracker",
    "identity_from_a2a_card",
]
