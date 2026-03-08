"""Tests for PolicyMetadata, PolicyRule, and PolicyBundle (bundle.py)."""

from __future__ import annotations

import hashlib

import pytest

from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(policy_id: str = "p1", content_hash: str = "") -> PolicyMetadata:
    return PolicyMetadata(policy_id=policy_id, content_hash=content_hash)


def _rule(
    rule_id: str = "r1",
    rule_type: str = "budget",
    enabled: bool = True,
    priority: int = 100,
) -> PolicyRule:
    return PolicyRule(rule_id=rule_id, rule_type=rule_type, enabled=enabled, priority=priority)


def _bundle_with_hash(*rules: PolicyRule, policy_id: str = "p1") -> PolicyBundle:
    """Create a bundle whose metadata.content_hash matches its rules."""
    r_tuple = tuple(rules)
    canonical = _canonical_rules_json(r_tuple)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=h)
    return PolicyBundle(metadata=meta, rules=r_tuple)


# ---------------------------------------------------------------------------
# PolicyMetadata
# ---------------------------------------------------------------------------


def test_metadata_valid_creation() -> None:
    meta = PolicyMetadata(policy_id="test-policy", version="2.0", epoch=5)
    assert meta.policy_id == "test-policy"
    assert meta.version == "2.0"
    assert meta.epoch == 5


def test_metadata_empty_id_rejected() -> None:
    with pytest.raises(ValueError, match="policy_id"):
        PolicyMetadata(policy_id="")


def test_metadata_negative_epoch_rejected() -> None:
    with pytest.raises(ValueError, match="epoch"):
        PolicyMetadata(policy_id="p1", epoch=-1)


# ---------------------------------------------------------------------------
# PolicyRule
# ---------------------------------------------------------------------------


def test_rule_valid_creation() -> None:
    rule = PolicyRule(
        rule_id="budget-limit",
        rule_type="budget",
        parameters={"max_usd": 1.0},
        enabled=True,
        priority=50,
    )
    assert rule.rule_id == "budget-limit"
    assert rule.rule_type == "budget"
    assert rule.parameters["max_usd"] == 1.0
    assert rule.priority == 50


def test_rule_empty_id_rejected() -> None:
    with pytest.raises(ValueError, match="rule_id"):
        PolicyRule(rule_id="", rule_type="budget")


def test_rule_empty_type_rejected() -> None:
    with pytest.raises(ValueError, match="rule_type"):
        PolicyRule(rule_id="r1", rule_type="")


# ---------------------------------------------------------------------------
# PolicyBundle.content_hash
# ---------------------------------------------------------------------------


def test_bundle_content_hash_deterministic() -> None:
    """Same rules in any insertion order must produce the same content hash."""
    r1 = _rule("r1", "budget")
    r2 = _rule("r2", "step")

    b_ab = PolicyBundle(metadata=_meta(), rules=(r1, r2))
    b_ba = PolicyBundle(metadata=_meta(), rules=(r2, r1))

    assert b_ab.content_hash() == b_ba.content_hash()


def test_bundle_verify_content_hash_valid() -> None:
    bundle = _bundle_with_hash(_rule("r1"), _rule("r2"))
    assert bundle.verify_content_hash() is True


def test_bundle_verify_content_hash_mismatch() -> None:
    r = _rule("r1")
    # Use a deliberately wrong hash.
    meta = PolicyMetadata(policy_id="p1", content_hash="deadbeef" * 8)
    bundle = PolicyBundle(metadata=meta, rules=(r,))
    assert bundle.verify_content_hash() is False


def test_bundle_verify_content_hash_empty_hash() -> None:
    """Empty content_hash should return False (not verified)."""
    bundle = PolicyBundle(metadata=_meta(content_hash=""), rules=(_rule("r1"),))
    assert bundle.verify_content_hash() is False


# ---------------------------------------------------------------------------
# PolicyBundle.active_rules
# ---------------------------------------------------------------------------


def test_bundle_active_rules_sorted_by_priority() -> None:
    r_high = _rule("r-high", "budget", priority=200)
    r_low = _rule("r-low", "step", priority=10)
    r_mid = _rule("r-mid", "retry", priority=100)
    bundle = PolicyBundle(metadata=_meta(), rules=(r_high, r_low, r_mid))

    active = bundle.active_rules
    priorities = [r.priority for r in active]
    assert priorities == sorted(priorities)
    assert active[0].rule_id == "r-low"


def test_bundle_disabled_rules_excluded() -> None:
    r_on = _rule("r-on", enabled=True)
    r_off = _rule("r-off", enabled=False)
    bundle = PolicyBundle(metadata=_meta(), rules=(r_on, r_off))

    active = bundle.active_rules
    assert len(active) == 1
    assert active[0].rule_id == "r-on"


# ---------------------------------------------------------------------------
# PolicyBundle.is_signed
# ---------------------------------------------------------------------------


def test_bundle_is_signed() -> None:
    unsigned = PolicyBundle(metadata=_meta(), rules=())
    signed = PolicyBundle(metadata=_meta(), rules=(), signature="abc123")

    assert unsigned.is_signed is False
    assert signed.is_signed is True
