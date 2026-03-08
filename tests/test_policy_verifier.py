"""Tests for PolicyVerifier and VerificationResult (verifier.py)."""

from __future__ import annotations

import hashlib


from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)
from veronica_core.policy.verifier import PolicyVerifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(rule_id: str, rule_type: str = "budget") -> PolicyRule:
    return PolicyRule(rule_id=rule_id, rule_type=rule_type)


def _meta(content_hash: str = "") -> PolicyMetadata:
    return PolicyMetadata(policy_id="p1", content_hash=content_hash)


def _bundle_with_hash(*rules: PolicyRule) -> PolicyBundle:
    r_tuple = tuple(rules)
    canonical = _canonical_rules_json(r_tuple)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id="p1", content_hash=h)
    return PolicyBundle(metadata=meta, rules=r_tuple)


# ---------------------------------------------------------------------------
# Basic pass / fail
# ---------------------------------------------------------------------------


def test_valid_bundle_passes() -> None:
    bundle = _bundle_with_hash(_rule("r1"), _rule("r2", "step"))
    result = PolicyVerifier().verify(bundle)
    assert result.valid is True
    assert result.errors == ()


def test_content_hash_mismatch_fails() -> None:
    meta = PolicyMetadata(policy_id="p1", content_hash="deadbeef" * 8)
    bundle = PolicyBundle(metadata=meta, rules=(_rule("r1"),))
    result = PolicyVerifier().verify(bundle)
    assert result.valid is False
    assert any("hash" in e.lower() for e in result.errors)


def test_unknown_rule_type_fails() -> None:
    bundle = _bundle_with_hash(_rule("r1", "nonexistent_type"))
    result = PolicyVerifier().verify(bundle)
    assert result.valid is False
    assert any("nonexistent_type" in e for e in result.errors)


def test_duplicate_rule_id_fails() -> None:
    r1a = PolicyRule(rule_id="r1", rule_type="budget", priority=10)
    r1b = PolicyRule(rule_id="r1", rule_type="step", priority=20)
    canonical = _canonical_rules_json((r1a, r1b))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id="p1", content_hash=h)
    bundle = PolicyBundle(metadata=meta, rules=(r1a, r1b))
    result = PolicyVerifier().verify(bundle)
    assert result.valid is False
    assert any("r1" in e for e in result.errors)


def test_signature_required_but_missing() -> None:
    bundle = _bundle_with_hash(_rule("r1"))
    result = PolicyVerifier(require_signature=True).verify(bundle)
    assert result.valid is False
    assert any("unsigned" in e.lower() or "require" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# VerificationResult fields
# ---------------------------------------------------------------------------


def test_verification_result_fields() -> None:
    bundle = _bundle_with_hash()
    result = PolicyVerifier().verify(bundle)
    assert isinstance(result.valid, bool)
    assert isinstance(result.errors, tuple)
    assert isinstance(result.warnings, tuple)
    assert isinstance(result.verified_at, float)
    assert result.verified_at > 0


# ---------------------------------------------------------------------------
# Custom allowed types
# ---------------------------------------------------------------------------


def test_custom_allowed_types() -> None:
    """A bundle using 'budget' should be rejected when only 'custom' is allowed."""
    bundle = _bundle_with_hash(_rule("r1", "budget"))
    result = PolicyVerifier(allowed_rule_types=frozenset({"custom"})).verify(bundle)
    assert result.valid is False
    assert any("budget" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Signature verification with signer
# ---------------------------------------------------------------------------


class _PassingSigner:
    def verify_bundle(self, bundle: PolicyBundle) -> bool:
        return True


class _FailingSigner:
    def verify_bundle(self, bundle: PolicyBundle) -> bool:
        return False


def test_all_checks_pass_with_signature() -> None:
    r = _rule("r1", "budget")
    canonical = _canonical_rules_json((r,))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id="p1", content_hash=h)
    bundle = PolicyBundle(metadata=meta, rules=(r,), signature="some-valid-sig")
    result = PolicyVerifier(
        require_signature=True,
        signer=_PassingSigner(),
    ).verify(bundle)
    assert result.valid is True
    assert result.errors == ()


def test_signer_failure_produces_error() -> None:
    r = _rule("r1", "budget")
    canonical = _canonical_rules_json((r,))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id="p1", content_hash=h)
    bundle = PolicyBundle(metadata=meta, rules=(r,), signature="bad-sig")
    result = PolicyVerifier(signer=_FailingSigner()).verify(bundle)
    assert result.valid is False
    assert any("verification failed" in e.lower() for e in result.errors)
