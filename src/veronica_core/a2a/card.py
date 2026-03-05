"""Utility for converting A2A Agent Cards to AgentIdentity."""

from __future__ import annotations

from typing import Any

from veronica_core.a2a.types import AgentIdentity, TrustLevel


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
    name = card.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(
            "A2A Agent Card must contain a non-empty 'name' string, "
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
