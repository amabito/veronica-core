"""Utility for converting A2A Agent Cards to AgentIdentity."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.types import AgentIdentity, TrustLevel


@runtime_checkable
class CardVerifierProtocol(Protocol):
    """Protocol for A2A Agent Card signature verifiers.

    Implementations check cryptographic signatures on Agent Cards
    (e.g. JWS verification via a2a-sdk or a custom PKI).
    """

    def verify(self, card: dict[str, Any]) -> bool:
        """Return True if the card's signature is valid."""
        ...

    def get_algorithm(self, card: dict[str, Any]) -> str | None:
        """Return the signature algorithm (e.g. 'RS256'), or None."""
        ...


def identity_from_a2a_card(card: dict[str, Any]) -> AgentIdentity:
    """Extract an :class:`AgentIdentity` from an A2A Agent Card dict.

    The A2A protocol defines an Agent Card as a JSON object that describes
    an agent's capabilities and identity.  This helper extracts the fields
    relevant for trust classification.

    Recognised card fields:

    * ``name`` (str, required) -- maps to ``agent_id``.
    * ``url`` (str, optional) -- stored in ``metadata["url"]`` if present.
    * ``trust_level`` (str, optional) -- one of the :class:`TrustLevel` values.
      Defaults to ``UNTRUSTED`` if missing or unrecognised.

    Args:
        card: A2A Agent Card dict. Must contain at least a ``"name"`` key.

    Returns:
        AgentIdentity with ``origin="a2a"``.

    Raises:
        ValueError: If ``card`` is missing the ``"name"`` key or it is empty.
    """
    if not isinstance(card, dict):
        raise ValueError(
            f"A2A Agent Card must be a dict, got {type(card).__name__}"
        )
    name = card.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        raise ValueError(
            "A2A Agent Card must contain a non-empty, non-whitespace 'name' string, "
            f"got {name!r}"
        )

    raw_trust = card.get("trust_level", "")
    try:
        trust = TrustLevel(raw_trust) if raw_trust else TrustLevel.UNTRUSTED
    except ValueError:
        trust = TrustLevel.UNTRUSTED
    # L-3: PRIVILEGED trust must never be granted via an external A2A card.
    # Callers who genuinely need PRIVILEGED must set it programmatically after
    # explicit admin review.  Silently downgrade rather than raising to avoid
    # crashing on cards from future protocol versions.
    if trust == TrustLevel.PRIVILEGED:
        trust = TrustLevel.UNTRUSTED

    metadata: dict[str, Any] = {}
    url = card.get("url")
    if url and isinstance(url, str):
        metadata["url"] = url

    return AgentIdentity(
        agent_id=name,
        origin="a2a",
        trust_level=trust,
        metadata=metadata,
    )


def verify_card_signature(
    card: dict[str, Any],
    *,
    verifier: CardVerifierProtocol | None = None,
    card_url: str | None = None,
) -> A2AIdentityProvenance:
    """Verify an A2A Agent Card's signature and return provenance metadata.

    Computes a stable fingerprint for the card regardless of whether a
    verifier is supplied.  The fingerprint is SHA-256 of the canonical
    JSON serialization (keys sorted, UTF-8 encoded).

    Args:
        card: A2A Agent Card dict.
        verifier: Optional signature verifier.  If None, the card's
            ``"signature"`` value must be a non-empty string for
            ``card_verified=True``.  This aligns with DefaultCardVerifier
            semantics.  Supply a real *verifier* for cryptographic guarantees.
            If the card cannot be serialized to JSON (non-serializable values),
            ``card_verified`` is forced to ``False`` (fail-closed).
        card_url: Well-known URL where the card was fetched, if known.

    Returns:
        :class:`A2AIdentityProvenance` populated from the verification result.
    """
    _serialization_failed = False
    try:
        card_fingerprint = hashlib.sha256(
            json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        # Card contains non-JSON-serializable values (bytes, set, etc.).
        # Treat as unverifiable -- fingerprint is None, card_verified is False.
        card_fingerprint = None
        _serialization_failed = True

    if verifier is not None:
        try:
            card_verified = bool(verifier.verify(card))
            card_signature_alg = verifier.get_algorithm(card)
        except Exception:  # noqa: BLE001
            # Verifier failure (PKI unavailable, etc.) -- fail-closed: unverified.
            card_verified = False
            card_signature_alg = None
    else:
        # Fail-closed: require a non-empty string signature AND successful
        # serialization.  Matches DefaultCardVerifier semantics so that
        # callers get consistent results regardless of which path is used.
        sig = card.get("signature")
        card_verified = (
            bool(sig and isinstance(sig, str)) and not _serialization_failed
        )
        card_signature_alg = None

    return A2AIdentityProvenance(
        card_url=card_url,
        card_verified=card_verified,
        card_signature_alg=card_signature_alg,
        card_fingerprint=card_fingerprint,
    )
