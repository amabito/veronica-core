"""A2A identity provenance -- verification metadata without mutating AgentIdentity.

AgentIdentity is a frozen dataclass used widely across veronica-core.
Adding fields would break constructor callsites and equality semantics.

Instead, A2A-specific verification data lives in this companion type.
Adapters carry provenance alongside AgentIdentity, never inside it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class A2AIdentityProvenance:
    """Verification metadata for an A2A agent's identity.

    Carried alongside AgentIdentity by adapter code. Never stored
    inside AgentIdentity.metadata to avoid schema pollution.

    Attributes:
        card_url: Well-known URL where the Agent Card was fetched from.
            None if the card was provided out-of-band.
        card_verified: True if JWS signature verification passed.
        card_signature_alg: JWS algorithm used (e.g. "RS256", "ES256").
            None if no signature was present or verification was skipped.
        tenant_id: A2A tenant scope from the original request. Used as
            a key prefix for circuit breakers, rate limiters, and stats.
        card_fingerprint: Stable fingerprint of the Agent Card content.
            Used as circuit breaker key (per-endpoint, not per-agent-id).
            Typically SHA-256 of the canonical card JSON.
    """

    card_url: str | None = None
    card_verified: bool = False
    card_signature_alg: str | None = None
    tenant_id: str | None = None
    card_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.card_verified, bool):
            raise TypeError(
                f"card_verified must be bool, got {type(self.card_verified).__name__}"
            )
