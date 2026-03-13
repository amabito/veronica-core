"""Tests for veronica_core.a2a.provenance.A2AIdentityProvenance."""

from __future__ import annotations

import pytest

from veronica_core.a2a.provenance import A2AIdentityProvenance


class TestA2AIdentityProvenance:
    def test_default_construction(self) -> None:
        prov = A2AIdentityProvenance()
        assert prov.card_url is None
        assert prov.card_verified is False
        assert prov.card_signature_alg is None
        assert prov.tenant_id is None
        assert prov.card_fingerprint is None

    def test_full_construction(self) -> None:
        prov = A2AIdentityProvenance(
            card_url="https://example.com/.well-known/agent.json",
            card_verified=True,
            card_signature_alg="RS256",
            tenant_id="tenant-1",
            card_fingerprint="abc123",
        )
        assert prov.card_url == "https://example.com/.well-known/agent.json"
        assert prov.card_verified is True
        assert prov.card_signature_alg == "RS256"
        assert prov.tenant_id == "tenant-1"
        assert prov.card_fingerprint == "abc123"

    def test_frozen_immutable_card_url(self) -> None:
        prov = A2AIdentityProvenance(card_url="https://example.com")
        with pytest.raises((TypeError, AttributeError)):
            prov.card_url = "https://other.com"  # type: ignore[misc]

    def test_frozen_immutable_card_verified(self) -> None:
        prov = A2AIdentityProvenance(card_verified=True)
        with pytest.raises((TypeError, AttributeError)):
            prov.card_verified = False  # type: ignore[misc]

    def test_frozen_immutable_signature_alg(self) -> None:
        prov = A2AIdentityProvenance(card_signature_alg="RS256")
        with pytest.raises((TypeError, AttributeError)):
            prov.card_signature_alg = "ES256"  # type: ignore[misc]

    def test_equality(self) -> None:
        p1 = A2AIdentityProvenance(card_url="https://a.com", card_verified=True)
        p2 = A2AIdentityProvenance(card_url="https://a.com", card_verified=True)
        assert p1 == p2

    def test_inequality_different_url(self) -> None:
        p1 = A2AIdentityProvenance(card_url="https://a.com")
        p2 = A2AIdentityProvenance(card_url="https://b.com")
        assert p1 != p2

    def test_inequality_different_verified(self) -> None:
        p1 = A2AIdentityProvenance(card_verified=True)
        p2 = A2AIdentityProvenance(card_verified=False)
        assert p1 != p2

    def test_hash_stable(self) -> None:
        """Frozen dataclass is hashable."""
        prov = A2AIdentityProvenance(card_url="https://example.com", card_verified=True)
        h1 = hash(prov)
        h2 = hash(prov)
        assert h1 == h2

    def test_usable_as_dict_key(self) -> None:
        prov = A2AIdentityProvenance(card_fingerprint="abc123")
        d = {prov: "value"}
        assert d[prov] == "value"

    def test_verified_false_alg_none_combination(self) -> None:
        """card_verified=False with alg set is unusual but not rejected."""
        prov = A2AIdentityProvenance(card_verified=False, card_signature_alg="RS256")
        assert prov.card_verified is False
        assert prov.card_signature_alg == "RS256"

    def test_repr_contains_fields(self) -> None:
        prov = A2AIdentityProvenance(card_verified=True, card_signature_alg="RS256")
        r = repr(prov)
        assert "card_verified=True" in r
        assert "RS256" in r

    # Exported from a2a package
    def test_importable_from_a2a_package(self) -> None:
        from veronica_core.a2a import A2AIdentityProvenance as Imported
        assert Imported is A2AIdentityProvenance


# ---------------------------------------------------------------------------
# Serialization round-trip (Rule 26)
# ---------------------------------------------------------------------------


class TestProvenanceRoundTrip:
    @pytest.mark.parametrize("bad_value", [1, 0, "yes", None, "true"])
    def test_non_bool_card_verified_rejected(self, bad_value: object) -> None:
        """__post_init__ must reject non-bool card_verified (Rule 18)."""
        with pytest.raises(TypeError, match="card_verified must be bool"):
            A2AIdentityProvenance(card_verified=bad_value)  # type: ignore[arg-type]

    def test_asdict_round_trip(self) -> None:
        from dataclasses import asdict

        original = A2AIdentityProvenance(
            card_url="https://example.com/.well-known/agent.json",
            card_verified=True,
            card_signature_alg="RS256",
            card_fingerprint="abc123def456",
        )
        data = asdict(original)
        restored = A2AIdentityProvenance(**data)
        assert restored == original
        assert type(restored.card_verified) is bool

    def test_asdict_with_none_fields(self) -> None:
        from dataclasses import asdict

        original = A2AIdentityProvenance()
        data = asdict(original)
        restored = A2AIdentityProvenance(**data)
        assert restored == original
