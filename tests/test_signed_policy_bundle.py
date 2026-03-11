"""Tests for signed PolicyBundle: PolicySigner.sign_bundle, verify_bundle,
and PolicyVerifier.verify_or_halt.
"""

from __future__ import annotations

import hashlib

import pytest

from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.verifier import PolicyVerifier
from veronica_core.security.policy_signing import PolicySigner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_KEY = hashlib.sha256(b"test-key-for-unit-tests").digest()


def _make_signer(key: bytes = _TEST_KEY) -> PolicySigner:
    return PolicySigner(key=key)


def _make_bundle(
    rules: tuple[PolicyRule, ...] = (),
    signature: str = "",
    with_content_hash: bool = True,
) -> PolicyBundle:
    content_hash = ""
    # Build a temporary bundle to compute its hash.
    tmp = PolicyBundle(
        metadata=PolicyMetadata(policy_id="test-policy"),
        rules=rules,
    )
    if with_content_hash:
        content_hash = tmp.content_hash()

    return PolicyBundle(
        metadata=PolicyMetadata(
            policy_id="test-policy",
            content_hash=content_hash,
        ),
        rules=rules,
        signature=signature,
    )


def _signed_bundle(
    signer: PolicySigner,
    rules: tuple[PolicyRule, ...] = (),
) -> PolicyBundle:
    """Create a bundle and sign it with the given signer."""
    unsigned = _make_bundle(rules=rules)
    sig = signer.sign_bundle(unsigned)
    return PolicyBundle(
        metadata=unsigned.metadata,
        rules=unsigned.rules,
        signature=sig,
    )


_RULE = PolicyRule(rule_id="r1", rule_type="budget")

# ---------------------------------------------------------------------------
# sign_bundle / verify_bundle roundtrip
# ---------------------------------------------------------------------------


def test_sign_and_verify_bundle_roundtrip() -> None:
    signer = _make_signer()
    bundle = _signed_bundle(signer, rules=(_RULE,))
    assert bundle.is_signed
    assert signer.verify_bundle(bundle) is True


def test_verify_bundle_wrong_key_returns_false() -> None:
    signer = _make_signer()
    bundle = _signed_bundle(signer, rules=(_RULE,))
    # Different key must not verify.
    other_signer = _make_signer(key=hashlib.sha256(b"wrong-key").digest())
    assert other_signer.verify_bundle(bundle) is False


def test_verify_bundle_empty_signature_returns_false() -> None:
    signer = _make_signer()
    bundle = _make_bundle(rules=(_RULE,), signature="")
    assert signer.verify_bundle(bundle) is False


def test_verify_bundle_tampered_content_returns_false() -> None:
    signer = _make_signer()
    bundle = _signed_bundle(signer, rules=(_RULE,))
    # Rebuild the bundle with an extra rule -- content_hash changes.
    extra_rule = PolicyRule(rule_id="r2", rule_type="shell")
    tampered = PolicyBundle(
        metadata=bundle.metadata,
        rules=(_RULE, extra_rule),
        signature=bundle.signature,  # original sig still present
    )
    assert signer.verify_bundle(tampered) is False


# ---------------------------------------------------------------------------
# PolicyVerifier.verify_or_halt
# ---------------------------------------------------------------------------


def test_verify_or_halt_valid_signed_bundle() -> None:
    signer = _make_signer()
    bundle = _signed_bundle(signer, rules=(_RULE,))
    result = PolicyVerifier.verify_or_halt(bundle, signer=signer)
    assert result.valid is True
    assert result.errors == ()


def test_verify_or_halt_unsigned_bundle_fails() -> None:
    bundle = _make_bundle(rules=(_RULE,))
    # No signer -- fail-closed: signer=None always produces invalid.
    result = PolicyVerifier.verify_or_halt(bundle)
    assert result.valid is False
    assert any("signer" in e.lower() or "signature" in e.lower() for e in result.errors)


def test_verify_or_halt_invalid_signature_fails() -> None:
    signer = _make_signer()
    bundle = _make_bundle(rules=(_RULE,), signature="deadbeef" * 8)
    result = PolicyVerifier.verify_or_halt(bundle, signer=signer)
    assert result.valid is False


def test_verify_or_halt_exception_in_signer_returns_invalid() -> None:
    class BrokenSigner:
        def verify_bundle(self, bundle: object) -> bool:
            raise RuntimeError("boom")

    bundle = _make_bundle(rules=(_RULE,), signature="deadbeef" * 8)
    result = PolicyVerifier.verify_or_halt(bundle, signer=BrokenSigner())
    assert result.valid is False


# ---------------------------------------------------------------------------
# Adversarial cases
# ---------------------------------------------------------------------------


def test_verify_or_halt_empty_rules_bundle() -> None:
    """Empty-rules bundle is structurally valid but must still be signed."""
    signer = _make_signer()
    bundle = _signed_bundle(signer, rules=())
    result = PolicyVerifier.verify_or_halt(bundle, signer=signer)
    assert result.valid is True


def test_verify_or_halt_none_signer() -> None:
    """None signer with require_signature=True must reject unsigned bundle."""
    bundle = _make_bundle()
    result = PolicyVerifier.verify_or_halt(bundle, signer=None)
    assert result.valid is False


def test_verify_bundle_corrupt_hash_field() -> None:
    """A bundle whose signature is all-zero hex must not verify."""
    signer = _make_signer()
    bundle = _make_bundle(signature="0" * 64)
    assert signer.verify_bundle(bundle) is False


def test_sign_bundle_deterministic() -> None:
    """Same key + same content_hash must always produce the same signature."""
    signer = _make_signer()
    bundle = _make_bundle(rules=(_RULE,))
    sig1 = signer.sign_bundle(bundle)
    sig2 = signer.sign_bundle(bundle)
    assert sig1 == sig2


def test_sign_bundle_different_rules_different_sig() -> None:
    """Different rules must produce different signatures."""
    signer = _make_signer()
    b1 = _make_bundle(rules=(_RULE,))
    b2 = _make_bundle(rules=(PolicyRule(rule_id="other", rule_type="shell"),))
    assert signer.sign_bundle(b1) != signer.sign_bundle(b2)


# ---------------------------------------------------------------------------
# H3: Canonical form newline injection (adversarial)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_id", "foo\nepoch=999"),
        ("issuer", "evil\nversion=99.0"),
        ("version", "1.0\ncontent_hash=deadbeef"),
        ("policy_id", "foo\repoch=999"),
    ],
    ids=["policy_id_lf", "issuer_lf", "version_lf", "policy_id_cr"],
)
def test_sign_bundle_rejects_newline_injection(field: str, value: str) -> None:
    """Newline in metadata string fields must be rejected (canonical injection)."""
    signer = _make_signer()
    kwargs = {"policy_id": "test-policy"}
    kwargs[field] = value
    bundle = PolicyBundle(
        metadata=PolicyMetadata(**kwargs),
        rules=(_RULE,),
    )
    with pytest.raises(ValueError, match="newline"):
        signer.sign_bundle(bundle)
