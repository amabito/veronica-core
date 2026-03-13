"""Tests for veronica_core.a2a.card -- verify_card_signature, identity_from_a2a_card."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from veronica_core.a2a.card import CardVerifierProtocol, identity_from_a2a_card, verify_card_signature
from veronica_core.a2a.provenance import A2AIdentityProvenance
from veronica_core.a2a.types import TrustLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fingerprint(card: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _TrueVerifier:
    """Always passes signature verification."""

    def verify(self, card: dict[str, Any]) -> bool:
        return True

    def get_algorithm(self, card: dict[str, Any]) -> str | None:
        return "RS256"


class _FalseVerifier:
    """Always fails signature verification."""

    def verify(self, card: dict[str, Any]) -> bool:
        return False

    def get_algorithm(self, card: dict[str, Any]) -> str | None:
        return "ES256"


class _NoneAlgVerifier:
    """Verifies OK but returns no algorithm."""

    def verify(self, card: dict[str, Any]) -> bool:
        return True

    def get_algorithm(self, card: dict[str, Any]) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestVerifyCardSignatureHappyPath:
    def test_card_with_signature_key_no_verifier(self) -> None:
        card = {"name": "agent-1", "signature": "abc123"}
        result = verify_card_signature(card)
        assert isinstance(result, A2AIdentityProvenance)
        assert result.card_verified is True
        assert result.card_signature_alg is None

    def test_card_without_signature_key_no_verifier(self) -> None:
        card = {"name": "agent-1"}
        result = verify_card_signature(card)
        assert result.card_verified is False
        assert result.card_signature_alg is None

    def test_custom_verifier_passes(self) -> None:
        card = {"name": "agent-1", "signature": "xyz"}
        result = verify_card_signature(card, verifier=_TrueVerifier())
        assert result.card_verified is True
        assert result.card_signature_alg == "RS256"

    def test_custom_verifier_fails(self) -> None:
        card = {"name": "agent-1", "signature": "bad"}
        result = verify_card_signature(card, verifier=_FalseVerifier())
        assert result.card_verified is False
        assert result.card_signature_alg == "ES256"

    def test_card_url_passthrough(self) -> None:
        card = {"name": "agent-1"}
        result = verify_card_signature(card, card_url="https://example.com/.well-known/agent.json")
        assert result.card_url == "https://example.com/.well-known/agent.json"

    def test_card_url_none_default(self) -> None:
        card = {"name": "agent-1"}
        result = verify_card_signature(card)
        assert result.card_url is None

    def test_verifier_none_alg_propagated(self) -> None:
        card = {"name": "agent-1"}
        result = verify_card_signature(card, verifier=_NoneAlgVerifier())
        assert result.card_verified is True
        assert result.card_signature_alg is None


# ---------------------------------------------------------------------------
# Fingerprint determinism
# ---------------------------------------------------------------------------


class TestCardFingerprintDeterminism:
    def test_fingerprint_matches_sha256(self) -> None:
        card = {"name": "agent-1", "url": "https://example.com"}
        result = verify_card_signature(card)
        assert result.card_fingerprint == _fingerprint(card)

    def test_fingerprint_is_deterministic_for_same_card(self) -> None:
        card = {"name": "agent-1", "url": "https://example.com"}
        r1 = verify_card_signature(card)
        r2 = verify_card_signature(card)
        assert r1.card_fingerprint == r2.card_fingerprint

    def test_fingerprint_differs_for_different_cards(self) -> None:
        card_a = {"name": "agent-1"}
        card_b = {"name": "agent-2"}
        r_a = verify_card_signature(card_a)
        r_b = verify_card_signature(card_b)
        assert r_a.card_fingerprint != r_b.card_fingerprint

    def test_fingerprint_stable_across_key_order(self) -> None:
        """sort_keys=True means key insertion order does not affect fingerprint."""
        card_ordered = {"a": 1, "b": 2, "name": "agent-1"}
        card_reversed = {"name": "agent-1", "b": 2, "a": 1}
        r1 = verify_card_signature(card_ordered)
        r2 = verify_card_signature(card_reversed)
        assert r1.card_fingerprint == r2.card_fingerprint

    def test_fingerprint_is_hex_string(self) -> None:
        card = {"name": "agent-1"}
        result = verify_card_signature(card)
        fp = result.card_fingerprint
        assert fp is not None
        assert len(fp) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# Adversarial: corrupted / edge-case card inputs
# ---------------------------------------------------------------------------


class TestVerifyCardSignatureAdversarial:
    def test_empty_dict_no_crash(self) -> None:
        result = verify_card_signature({})
        assert result.card_verified is False
        assert result.card_fingerprint is not None

    def test_none_value_in_card_no_crash(self) -> None:
        card = {"name": "agent-1", "extra": None}
        result = verify_card_signature(card)
        assert result.card_fingerprint is not None

    def test_deeply_nested_card_no_crash(self) -> None:
        card: dict[str, Any] = {"name": "agent-1"}
        node: dict[str, Any] = card
        for _ in range(50):
            node["child"] = {}
            node = node["child"]
        result = verify_card_signature(card)
        assert result.card_fingerprint is not None

    def test_large_card_no_crash(self) -> None:
        card = {"name": "agent-1", **{f"key_{i}": f"value_{i}" for i in range(1000)}}
        result = verify_card_signature(card)
        assert result.card_fingerprint is not None

    @pytest.mark.parametrize("sig_value", [None, "", 0, False, {}, []])
    def test_non_string_signature_values_rejected(self, sig_value: object) -> None:
        """Non-string or falsy signature must yield card_verified=False.

        Aligned with DefaultCardVerifier: signature must be a non-empty string.
        """
        card = {"name": "agent-1", "signature": sig_value}
        result = verify_card_signature(card)
        assert result.card_verified is False

    def test_signature_absent_after_pop_no_verifier(self) -> None:
        card = {"name": "agent-1", "signature": "abc"}
        card_no_sig = {k: v for k, v in card.items() if k != "signature"}
        result = verify_card_signature(card_no_sig)
        assert result.card_verified is False

    def test_verifier_called_on_empty_dict(self) -> None:
        result = verify_card_signature({}, verifier=_TrueVerifier())
        assert result.card_verified is True

    def test_return_type_is_provenance(self) -> None:
        result = verify_card_signature({"name": "x"})
        assert type(result) is A2AIdentityProvenance


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestCardVerifierProtocol:
    def test_true_verifier_satisfies_protocol(self) -> None:
        assert isinstance(_TrueVerifier(), CardVerifierProtocol)

    def test_false_verifier_satisfies_protocol(self) -> None:
        assert isinstance(_FalseVerifier(), CardVerifierProtocol)

    def test_object_without_verify_does_not_satisfy_protocol(self) -> None:
        class NoVerify:
            def get_algorithm(self, card: dict[str, Any]) -> str | None:
                return None

        assert not isinstance(NoVerify(), CardVerifierProtocol)

    def test_object_without_get_algorithm_does_not_satisfy_protocol(self) -> None:
        class NoAlg:
            def verify(self, card: dict[str, Any]) -> bool:
                return True

        assert not isinstance(NoAlg(), CardVerifierProtocol)


# ---------------------------------------------------------------------------
# Non-JSON-serializable card (TypeError handling)
# ---------------------------------------------------------------------------


class TestCardVerificationNonSerializable:
    def test_non_serializable_card_does_not_crash(self) -> None:
        """Card with bytes/set values must not crash verify_card_signature."""
        prov = verify_card_signature({"name": "agent", "data": b"bytes"})
        assert prov.card_fingerprint is None
        assert prov.card_verified is False  # "signature" key not present

    def test_non_serializable_card_with_signature(self) -> None:
        """Non-serializable card with signature key: fail-closed, verified=False."""
        prov = verify_card_signature({"name": "agent", "signature": "abc", "data": {1, 2, 3}})
        assert prov.card_fingerprint is None
        assert prov.card_verified is False  # fail-closed: no fingerprint -> unverified

    def test_verifier_that_raises_fails_closed(self) -> None:
        """Verifier that raises must result in card_verified=False."""

        class _RaisingVerifier:
            def verify(self, card: dict[str, Any]) -> bool:
                raise RuntimeError("PKI unavailable")

            def get_algorithm(self, card: dict[str, Any]) -> str | None:
                raise RuntimeError("PKI unavailable")

        prov = verify_card_signature(
            {"name": "agent", "signature": "valid"},
            verifier=_RaisingVerifier(),
        )
        assert prov.card_verified is False
        assert prov.card_signature_alg is None


# ---------------------------------------------------------------------------
# identity_from_a2a_card
# ---------------------------------------------------------------------------


class TestIdentityFromA2ACard:
    def test_valid_card_returns_identity(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1"})
        assert identity.agent_id == "agent-1"
        assert identity.origin == "a2a"
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            identity_from_a2a_card({})

    def test_non_string_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            identity_from_a2a_card({"name": 123})

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            identity_from_a2a_card({"name": ""})

    def test_whitespace_only_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            identity_from_a2a_card({"name": "   "})

    def test_privileged_trust_downgraded_to_untrusted(self) -> None:
        """PRIVILEGED must never be granted via external A2A card (L-3 rule)."""
        identity = identity_from_a2a_card({"name": "agent-1", "trust_level": "privileged"})
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_unrecognised_trust_level_defaults_untrusted(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1", "trust_level": "SUPERADMIN"})
        assert identity.trust_level == TrustLevel.UNTRUSTED

    def test_valid_trust_level_accepted(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1", "trust_level": "trusted"})
        assert identity.trust_level == TrustLevel.TRUSTED

    def test_url_stored_in_metadata(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1", "url": "https://example.com"})
        assert identity.metadata.get("url") == "https://example.com"

    def test_non_string_url_ignored(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1", "url": 12345})
        assert "url" not in identity.metadata

    def test_no_url_no_metadata_url(self) -> None:
        identity = identity_from_a2a_card({"name": "agent-1"})
        assert "url" not in identity.metadata
