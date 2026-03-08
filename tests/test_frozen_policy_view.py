"""Tests for FrozenPolicyView and PolicyViewHolder (frozen_view.py)."""

from __future__ import annotations

import hashlib
import threading
from typing import Any

import pytest

from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(rule_id: str, rule_type: str = "budget") -> PolicyRule:
    return PolicyRule(rule_id=rule_id, rule_type=rule_type)


def _valid_bundle(*rules: PolicyRule, policy_id: str = "p1") -> PolicyBundle:
    r_tuple = tuple(rules) if rules else (_rule("default"),)
    canonical = _canonical_rules_json(r_tuple)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=h)
    return PolicyBundle(metadata=meta, rules=r_tuple)


def _valid_result() -> VerificationResult:
    return VerificationResult(valid=True, errors=(), warnings=())


def _invalid_result() -> VerificationResult:
    return VerificationResult(valid=False, errors=("forced error",), warnings=())


def _make_view(*rules: PolicyRule) -> FrozenPolicyView:
    bundle = _valid_bundle(*rules) if rules else _valid_bundle()
    return FrozenPolicyView(bundle, _valid_result())


# ---------------------------------------------------------------------------
# FrozenPolicyView construction
# ---------------------------------------------------------------------------


def test_creation_from_valid_bundle() -> None:
    bundle = _valid_bundle(_rule("r1"), _rule("r2", "step"))
    view = FrozenPolicyView(bundle, _valid_result())
    assert view.metadata.policy_id == "p1"
    assert len(view.rules) == 2


def test_creation_from_invalid_raises() -> None:
    bundle = _valid_bundle()
    with pytest.raises(ValueError, match="invalid"):
        FrozenPolicyView(bundle, _invalid_result())


# ---------------------------------------------------------------------------
# FrozenPolicyView query helpers
# ---------------------------------------------------------------------------


def test_rules_for_type_found() -> None:
    view = _make_view(_rule("r1", "budget"), _rule("r2", "budget"), _rule("r3", "step"))
    budget_rules = view.rules_for_type("budget")
    assert len(budget_rules) == 2
    assert all(r.rule_type == "budget" for r in budget_rules)


def test_rules_for_type_not_found() -> None:
    view = _make_view(_rule("r1", "budget"))
    assert view.rules_for_type("network") == ()


def test_has_rule_type() -> None:
    view = _make_view(_rule("r1", "budget"), _rule("r2", "step"))
    assert view.has_rule_type("budget") is True
    assert view.has_rule_type("network") is False


def test_rule_types_frozenset() -> None:
    view = _make_view(_rule("r1", "budget"), _rule("r2", "step"), _rule("r3", "step"))
    assert view.rule_types == frozenset({"budget", "step"})


# ---------------------------------------------------------------------------
# FrozenPolicyView.to_audit_dict
# ---------------------------------------------------------------------------


def test_to_audit_dict_fields() -> None:
    bundle = _valid_bundle(_rule("r1", "budget"), _rule("r2", "step"))
    view = FrozenPolicyView(bundle, _valid_result())
    d = view.to_audit_dict()

    assert d["policy_id"] == "p1"
    assert isinstance(d["version"], str)
    assert isinstance(d["epoch"], int)
    assert isinstance(d["is_signed"], bool)
    assert d["rule_count"] == 2
    assert "budget" in d["rule_types"]
    assert "step" in d["rule_types"]
    assert isinstance(d["verified_at"], float)


# ---------------------------------------------------------------------------
# PolicyViewHolder
# ---------------------------------------------------------------------------


def test_holder_initial_none() -> None:
    holder = PolicyViewHolder()
    assert holder.current is None


def test_holder_swap() -> None:
    holder = PolicyViewHolder()
    view = _make_view()
    old = holder.swap(view)
    assert old is None
    assert holder.current is view


def test_holder_load_valid_bundle() -> None:
    holder = PolicyViewHolder()
    bundle = _valid_bundle(_rule("r1"))
    result = holder.load_bundle(bundle)
    assert result.valid is True
    assert holder.current is not None
    assert holder.current.metadata.policy_id == "p1"


def test_holder_load_invalid_no_swap() -> None:
    """An invalid bundle must not replace the existing view."""
    existing_view = _make_view(_rule("r-existing"))
    holder = PolicyViewHolder(initial=existing_view)

    # Bundle with wrong content hash -- verification will fail.
    meta = PolicyMetadata(policy_id="bad", content_hash="deadbeef" * 8)
    bad_bundle = PolicyBundle(metadata=meta, rules=(_rule("r1"),))
    result = holder.load_bundle(bad_bundle)

    assert result.valid is False
    # Existing view must be unchanged.
    assert holder.current is existing_view


# ---------------------------------------------------------------------------
# PolicyViewHolder concurrency
# ---------------------------------------------------------------------------


def test_holder_concurrent_reads() -> None:
    """10 threads reading current simultaneously must all get the same view."""
    view = _make_view(_rule("r1"))
    holder = PolicyViewHolder(initial=view)

    results: list[Any] = []
    errors: list[Exception] = []

    def read() -> None:
        try:
            results.append(holder.current)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 10
    # All threads must have seen the same view object.
    assert all(r is view for r in results)
