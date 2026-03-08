"""Tests for enrich_audit_with_policy (audit_helpers.py)."""

from __future__ import annotations

import hashlib

from veronica_core.policy.audit_helpers import enrich_audit_with_policy
from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)
from veronica_core.policy.frozen_view import FrozenPolicyView
from veronica_core.policy.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view(policy_id: str = "p1") -> FrozenPolicyView:
    rule = PolicyRule(rule_id="r1", rule_type="budget")
    canonical = _canonical_rules_json((rule,))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=h)
    bundle = PolicyBundle(metadata=meta, rules=(rule,))
    result = VerificationResult(valid=True, errors=(), warnings=())
    return FrozenPolicyView(bundle, result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enrich_with_policy_view() -> None:
    view = _make_view()
    data: dict = {"event_type": "ALLOW", "cost": 0.01}
    enriched = enrich_audit_with_policy(data, view)

    assert enriched["event_type"] == "ALLOW"
    assert enriched["cost"] == 0.01
    assert enriched["policy"] is not None
    assert enriched["policy"]["policy_id"] == "p1"


def test_enrich_with_none_policy() -> None:
    data: dict = {"event_type": "DENY"}
    enriched = enrich_audit_with_policy(data, None)

    assert enriched["event_type"] == "DENY"
    assert enriched["policy"] is None


def test_enrich_preserves_existing_data() -> None:
    """enrich must not mutate the original dict."""
    original: dict = {"x": 1, "y": [2, 3]}
    view = _make_view()

    enriched = enrich_audit_with_policy(original, view)

    # New key added.
    assert "policy" in enriched
    # Original dict untouched.
    assert "policy" not in original
    assert original == {"x": 1, "y": [2, 3]}
