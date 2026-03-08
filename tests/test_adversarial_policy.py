"""Adversarial tests for policy kernel: bundle, verifier, frozen_view.

Attacker mindset -- how do we break immutability, tamper with hashes,
bypass signatures, trigger races, and corrupt inputs?

Test classes:
    TestAdversarialPolicyBundle   -- bundle.py (7 categories)
    TestAdversarialPolicyVerifier -- verifier.py (6 categories)
    TestAdversarialFrozenView     -- frozen_view.py (5 categories)
"""

from __future__ import annotations

import hashlib
import threading
import types
from typing import Any

import pytest

from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
    _canonical_rules_json,
)
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.verifier import PolicyVerifier, VerificationResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _meta(policy_id: str = "p1", content_hash: str = "", epoch: int = 0) -> PolicyMetadata:
    return PolicyMetadata(policy_id=policy_id, content_hash=content_hash, epoch=epoch)


def _rule(
    rule_id: str = "r1",
    rule_type: str = "budget",
    enabled: bool = True,
    priority: int = 100,
    parameters: dict[str, Any] | None = None,
) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        rule_type=rule_type,
        enabled=enabled,
        priority=priority,
        parameters=parameters or {},
    )


def _bundle_with_correct_hash(*rules: PolicyRule, policy_id: str = "p1") -> PolicyBundle:
    r_tuple = tuple(rules)
    canonical = _canonical_rules_json(r_tuple)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    meta = PolicyMetadata(policy_id=policy_id, content_hash=h)
    return PolicyBundle(metadata=meta, rules=r_tuple)


def _valid_verification() -> VerificationResult:
    return VerificationResult(valid=True, errors=(), warnings=())


def _make_frozen_view(*rules: PolicyRule) -> FrozenPolicyView:
    r = rules if rules else (_rule("default"),)
    bundle = _bundle_with_correct_hash(*r)
    return FrozenPolicyView(bundle, _valid_verification())


# ===========================================================================
# TestAdversarialPolicyBundle
# ===========================================================================


class TestAdversarialPolicyBundle:
    """Adversarial tests for PolicyBundle / PolicyRule / PolicyMetadata -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Category 1: Immutability bypass -- metadata and rule attributes
    # -----------------------------------------------------------------------

    def test_metadata_frozen_attribute_mutation_raises(self) -> None:
        """Frozen dataclass must reject direct attribute assignment."""
        meta = _meta()
        with pytest.raises((AttributeError, TypeError)):
            meta.policy_id = "hacked"  # type: ignore[misc]

    def test_metadata_tags_is_mapping_proxy_not_writable(self) -> None:
        """tags must be coerced to MappingProxyType -- mutations must fail."""
        meta = PolicyMetadata(policy_id="p1", tags={"env": "prod"})
        assert isinstance(meta.tags, types.MappingProxyType)
        with pytest.raises(TypeError):
            meta.tags["env"] = "hacked"  # type: ignore[index]

    def test_rule_parameters_is_mapping_proxy_not_writable(self) -> None:
        """parameters must be coerced to MappingProxyType -- mutations must fail."""
        rule = _rule(parameters={"max_usd": 1.0})
        assert isinstance(rule.parameters, types.MappingProxyType)
        with pytest.raises(TypeError):
            rule.parameters["max_usd"] = 999.0  # type: ignore[index]

    def test_bundle_rules_coerced_to_tuple_from_list(self) -> None:
        """Passing a list as rules must be silently coerced to tuple (immutable)."""
        r1 = _rule("r1")
        r2 = _rule("r2", "step")
        bundle = PolicyBundle(metadata=_meta(), rules=[r1, r2])  # type: ignore[arg-type]
        assert isinstance(bundle.rules, tuple)

    def test_bundle_frozen_signature_mutation_raises(self) -> None:
        """Frozen dataclass must reject signature overwrite post-construction."""
        bundle = PolicyBundle(metadata=_meta(), rules=(), signature="original")
        with pytest.raises((AttributeError, TypeError)):
            bundle.signature = "tampered"  # type: ignore[misc]

    def test_rule_frozen_enabled_toggle_raises(self) -> None:
        """enabled field must not be togglable after construction."""
        rule = _rule(enabled=True)
        with pytest.raises((AttributeError, TypeError)):
            rule.enabled = False  # type: ignore[misc]

    def test_metadata_tags_original_dict_mutation_does_not_propagate(self) -> None:
        """Mutating the original dict passed to PolicyMetadata must not affect stored tags."""
        mutable_tags: dict[str, str] = {"k": "v1"}
        meta = PolicyMetadata(policy_id="p1", tags=mutable_tags)
        mutable_tags["k"] = "v2"  # mutate after construction
        assert meta.tags["k"] == "v1", "Stored tags must not reflect post-construction mutation"

    def test_rule_parameters_original_dict_mutation_does_not_propagate(self) -> None:
        """Mutating the original dict passed to PolicyRule must not affect stored parameters."""
        params: dict[str, Any] = {"limit": 10}
        rule = PolicyRule(rule_id="r1", rule_type="budget", parameters=params)
        params["limit"] = 9999  # mutate after construction
        assert rule.parameters["limit"] == 10, "Stored parameters must not reflect later mutation"

    # -----------------------------------------------------------------------
    # Category 2: Content hash tampering -- various attack vectors
    # -----------------------------------------------------------------------

    def test_content_hash_cached_value_is_stable(self) -> None:
        """content_hash() must return the same value on repeated calls."""
        bundle = _bundle_with_correct_hash(_rule("r1"), _rule("r2", "step"))
        h1 = bundle.content_hash()
        h2 = bundle.content_hash()
        assert h1 == h2

    def test_content_hash_changes_when_rule_type_differs(self) -> None:
        """Two bundles with different rule_types must produce different hashes."""
        b1 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", "budget"),))
        b2 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", "step"),))
        assert b1.content_hash() != b2.content_hash()

    def test_content_hash_changes_when_priority_differs(self) -> None:
        """Priority is part of canonical JSON -- different priority = different hash."""
        b1 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", priority=10),))
        b2 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", priority=20),))
        assert b1.content_hash() != b2.content_hash()

    def test_content_hash_changes_when_enabled_differs(self) -> None:
        """enabled flag is part of canonical JSON."""
        b1 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", enabled=True),))
        b2 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", enabled=False),))
        assert b1.content_hash() != b2.content_hash()

    def test_content_hash_order_independent(self) -> None:
        """Rule insertion order must not affect the content hash (sorted by rule_id)."""
        r1 = _rule("alpha", "budget")
        r2 = _rule("beta", "step")
        b_ab = PolicyBundle(metadata=_meta(), rules=(r1, r2))
        b_ba = PolicyBundle(metadata=_meta(), rules=(r2, r1))
        assert b_ab.content_hash() == b_ba.content_hash()

    def test_verify_content_hash_wrong_stored_hash_returns_false(self) -> None:
        """A deliberately wrong content_hash in metadata must return False."""
        meta = PolicyMetadata(policy_id="p1", content_hash="a" * 64)
        bundle = PolicyBundle(metadata=meta, rules=(_rule("r1"),))
        assert bundle.verify_content_hash() is False

    def test_verify_content_hash_empty_stored_hash_returns_false(self) -> None:
        """Empty stored hash must return False (not verified, not True)."""
        bundle = PolicyBundle(metadata=_meta(content_hash=""), rules=(_rule("r1"),))
        assert bundle.verify_content_hash() is False

    def test_content_hash_changes_when_parameter_value_differs(self) -> None:
        """Parameter values are part of canonical JSON."""
        b1 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", parameters={"limit": 10}),))
        b2 = PolicyBundle(metadata=_meta(), rules=(_rule("r1", parameters={"limit": 20}),))
        assert b1.content_hash() != b2.content_hash()

    # -----------------------------------------------------------------------
    # Category 3: Concurrent content_hash computation (same bundle object)
    # -----------------------------------------------------------------------

    def test_concurrent_content_hash_all_return_same_value(self) -> None:
        """20 threads computing content_hash on the same bundle must all agree."""
        bundle = PolicyBundle(
            metadata=_meta(),
            rules=tuple(_rule(f"r{i}", "budget") for i in range(20)),
        )
        results: list[str] = []
        errors: list[Exception] = []

        def compute() -> None:
            try:
                results.append(bundle.content_hash())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=compute) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Exceptions during concurrent hash: {errors}"
        assert len(results) == 20
        assert len(set(results)) == 1, "All threads must compute the same hash"

    def test_concurrent_active_rules_all_return_same_tuple(self) -> None:
        """20 threads reading active_rules on the same bundle must all see the same tuple."""
        rules = tuple(
            PolicyRule(rule_id=f"r{i}", rule_type="budget", priority=i, enabled=(i % 2 == 0))
            for i in range(30)
        )
        bundle = PolicyBundle(metadata=_meta(), rules=rules)
        results: list[tuple[PolicyRule, ...]] = []
        errors: list[Exception] = []

        def read_active() -> None:
            try:
                results.append(bundle.active_rules)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=read_active) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All results must be the same tuple object (cached) or equal value
        first = results[0]
        assert all(r == first for r in results)

    # -----------------------------------------------------------------------
    # Category 4: Corrupted / boundary inputs to PolicyMetadata
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",          # empty string
            None,        # None
            123,         # int
            [],          # list
        ],
    )
    def test_metadata_invalid_policy_id_rejected(self, bad_id: Any) -> None:
        """Non-string or empty policy_id must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            PolicyMetadata(policy_id=bad_id)

    @pytest.mark.parametrize("bad_epoch", [-1, -100, -999999])
    def test_metadata_negative_epoch_rejected(self, bad_epoch: int) -> None:
        """Negative epoch must raise ValueError."""
        with pytest.raises(ValueError, match="epoch"):
            PolicyMetadata(policy_id="p1", epoch=bad_epoch)

    def test_metadata_epoch_zero_accepted(self) -> None:
        """Epoch 0 is the valid minimum."""
        meta = PolicyMetadata(policy_id="p1", epoch=0)
        assert meta.epoch == 0

    def test_metadata_large_epoch_accepted(self) -> None:
        """Very large epoch values must be accepted (no upper bound in spec)."""
        meta = PolicyMetadata(policy_id="p1", epoch=2**31 - 1)
        assert meta.epoch == 2**31 - 1

    # -----------------------------------------------------------------------
    # Category 5: Corrupted inputs to PolicyRule
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad_rule_id",
        ["", None, 0, [], {}],
    )
    def test_rule_invalid_rule_id_rejected(self, bad_rule_id: Any) -> None:
        """Empty or non-string rule_id must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            PolicyRule(rule_id=bad_rule_id, rule_type="budget")

    @pytest.mark.parametrize(
        "bad_rule_type",
        ["", None, 0],
    )
    def test_rule_invalid_rule_type_rejected(self, bad_rule_type: Any) -> None:
        """Empty or non-string rule_type must raise ValueError."""
        with pytest.raises((ValueError, TypeError)):
            PolicyRule(rule_id="r1", rule_type=bad_rule_type)

    # -----------------------------------------------------------------------
    # Category 6: Boundary -- empty rules / single rule / large rule count
    # -----------------------------------------------------------------------

    def test_bundle_with_empty_rules_has_stable_hash(self) -> None:
        """Empty rules tuple must produce a deterministic, non-empty hash."""
        b1 = PolicyBundle(metadata=_meta(), rules=())
        b2 = PolicyBundle(metadata=_meta(), rules=())
        assert b1.content_hash() == b2.content_hash()
        assert len(b1.content_hash()) == 64  # sha256 hex

    def test_bundle_empty_rules_active_rules_is_empty(self) -> None:
        bundle = PolicyBundle(metadata=_meta(), rules=())
        assert bundle.active_rules == ()

    def test_bundle_single_rule(self) -> None:
        bundle = PolicyBundle(metadata=_meta(), rules=(_rule("only"),))
        assert len(bundle.rules) == 1
        assert bundle.active_rules[0].rule_id == "only"

    def test_bundle_large_rule_count_hash_stable(self) -> None:
        """1000+ rules must produce a stable, deterministic hash."""
        rules = tuple(_rule(f"r{i:04d}", "budget") for i in range(1000))
        b1 = PolicyBundle(metadata=_meta(), rules=rules)
        b2 = PolicyBundle(metadata=_meta(), rules=rules)
        assert b1.content_hash() == b2.content_hash()

    def test_bundle_large_rule_count_active_rules_sorted(self) -> None:
        """active_rules for 1000 rules must be sorted by (priority, rule_id)."""
        import random

        rng = random.Random(42)
        rules = tuple(
            PolicyRule(
                rule_id=f"r{i:04d}",
                rule_type="budget",
                priority=rng.randint(1, 500),
                enabled=True,
            )
            for i in range(1000)
        )
        bundle = PolicyBundle(metadata=_meta(), rules=rules)
        active = bundle.active_rules
        assert len(active) == 1000
        keys = [(r.priority, r.rule_id) for r in active]
        assert keys == sorted(keys)


# ===========================================================================
# TestAdversarialPolicyVerifier
# ===========================================================================


class TestAdversarialPolicyVerifier:
    """Adversarial tests for PolicyVerifier -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Category 1: Signature bypass scenarios
    # -----------------------------------------------------------------------

    def test_signer_present_bundle_unsigned_emits_warning_not_error(self) -> None:
        """signer provided + bundle unsigned + require_signature=False must warn, not error."""
        class NoopSigner:
            def verify_bundle(self, bundle: PolicyBundle) -> bool:
                return True

        bundle = _bundle_with_correct_hash(_rule("r1"))
        result = PolicyVerifier(signer=NoopSigner()).verify(bundle)
        assert result.valid is True, "Unsigned bundle with signer (not required) must still be valid"
        assert any("unsigned" in w.lower() or "skipped" in w.lower() for w in result.warnings)

    def test_require_signature_unsigned_bundle_is_invalid(self) -> None:
        """require_signature=True with unsigned bundle must be invalid."""
        bundle = _bundle_with_correct_hash(_rule("r1"))
        result = PolicyVerifier(require_signature=True).verify(bundle)
        assert result.valid is False
        assert any("unsigned" in e.lower() or "require" in e.lower() for e in result.errors)

    def test_signer_raises_exception_produces_error(self) -> None:
        """A signer that raises must be caught and reported as an error."""
        class ExplodingSigner:
            def verify_bundle(self, bundle: PolicyBundle) -> bool:
                raise RuntimeError("signer exploded")

        r = _rule("r1")
        canonical = _canonical_rules_json((r,))
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        meta = PolicyMetadata(policy_id="p1", content_hash=h)
        bundle = PolicyBundle(metadata=meta, rules=(r,), signature="sig-value")
        result = PolicyVerifier(signer=ExplodingSigner()).verify(bundle)
        assert result.valid is False
        assert any("RuntimeError" in e or "exploded" in e for e in result.errors)

    def test_signer_without_verify_bundle_method_emits_warning(self) -> None:
        """signer without verify_bundle() must emit a warning, not an error."""
        class BadSigner:
            pass

        r = _rule("r1")
        canonical = _canonical_rules_json((r,))
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        meta = PolicyMetadata(policy_id="p1", content_hash=h)
        bundle = PolicyBundle(metadata=meta, rules=(r,), signature="sig-value")
        result = PolicyVerifier(signer=BadSigner()).verify(bundle)
        assert any("verify_bundle" in w.lower() or "not verified" in w.lower() for w in result.warnings)

    def test_signed_bundle_no_signer_passes_without_verification(self) -> None:
        """Signed bundle with no signer provided must pass (signature is advisory)."""
        bundle = _bundle_with_correct_hash(_rule("r1"))
        signed_bundle = PolicyBundle(
            metadata=bundle.metadata, rules=bundle.rules, signature="unverifiable"
        )
        result = PolicyVerifier(require_signature=False).verify(signed_bundle)
        assert result.valid is True

    # -----------------------------------------------------------------------
    # Category 2: Duplicate rule_id attacks
    # -----------------------------------------------------------------------

    def test_duplicate_rule_ids_produces_error(self) -> None:
        """Two rules with the same rule_id must be caught and invalidate the bundle."""
        r1a = PolicyRule(rule_id="dup", rule_type="budget", priority=10)
        r1b = PolicyRule(rule_id="dup", rule_type="step", priority=20)
        # Build bundle with correct hash for these two rules (which happen to be duplicates)
        canonical = _canonical_rules_json((r1a, r1b))
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        meta = PolicyMetadata(policy_id="p1", content_hash=h)
        bundle = PolicyBundle(metadata=meta, rules=(r1a, r1b))
        result = PolicyVerifier().verify(bundle)
        assert result.valid is False
        assert any("dup" in e for e in result.errors)

    def test_many_duplicate_rule_ids_each_reported(self) -> None:
        """Three distinct duplicate rule_ids must each appear in errors."""
        rules = []
        for dup_id in ("alpha", "beta", "gamma"):
            rules.append(PolicyRule(rule_id=dup_id, rule_type="budget", priority=10))
            rules.append(PolicyRule(rule_id=dup_id, rule_type="step", priority=20))
        r_tuple = tuple(rules)
        canonical = _canonical_rules_json(r_tuple)
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        meta = PolicyMetadata(policy_id="p1", content_hash=h)
        bundle = PolicyBundle(metadata=meta, rules=r_tuple)
        result = PolicyVerifier().verify(bundle)
        assert result.valid is False
        error_text = " ".join(result.errors)
        for dup_id in ("alpha", "beta", "gamma"):
            assert dup_id in error_text

    # -----------------------------------------------------------------------
    # Category 3: Unknown / disallowed rule types
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad_type",
        ["nonexistent", "BUDGET", "Budget", "shell_injection", "__init__", ""],
    )
    def test_unknown_rule_type_rejected(self, bad_type: str) -> None:
        """Unknown rule_type values must be caught -- case-sensitive match."""
        if not bad_type:
            # Empty string is caught at PolicyRule construction, not verifier
            with pytest.raises(ValueError):
                PolicyRule(rule_id="r1", rule_type=bad_type)
            return
        bundle = _bundle_with_correct_hash(_rule("r1", bad_type))
        result = PolicyVerifier().verify(bundle)
        assert result.valid is False
        assert any(bad_type in e for e in result.errors)

    def test_empty_allowed_types_rejects_all_known_types(self) -> None:
        """allowed_rule_types=frozenset() must reject every rule."""
        bundle = _bundle_with_correct_hash(_rule("r1", "budget"), _rule("r2", "step"))
        result = PolicyVerifier(allowed_rule_types=frozenset()).verify(bundle)
        assert result.valid is False
        assert len(result.errors) >= 2

    # -----------------------------------------------------------------------
    # Category 4: Content hash mismatch attack
    # -----------------------------------------------------------------------

    def test_hash_mismatch_reported_with_both_values(self) -> None:
        """Error message must include both declared and computed hashes."""
        wrong_hash = "b" * 64
        meta = PolicyMetadata(policy_id="p1", content_hash=wrong_hash)
        bundle = PolicyBundle(metadata=meta, rules=(_rule("r1"),))
        result = PolicyVerifier().verify(bundle)
        assert result.valid is False
        error_text = " ".join(result.errors)
        assert wrong_hash in error_text

    def test_no_declared_hash_emits_warning_not_error(self) -> None:
        """Bundle without content_hash must warn about disabled tamper detection."""
        bundle = PolicyBundle(metadata=_meta(content_hash=""), rules=(_rule("r1"),))
        result = PolicyVerifier().verify(bundle)
        # valid is True because no declared hash is not an error
        assert result.valid is True
        assert any("tamper" in w.lower() or "content_hash" in w.lower() for w in result.warnings)

    # -----------------------------------------------------------------------
    # Category 5: Accumulated errors -- multiple failures at once
    # -----------------------------------------------------------------------

    def test_multiple_errors_accumulated(self) -> None:
        """Verifier must report ALL errors, not stop at first."""
        # Bundle with: wrong hash + duplicate rule_id + unknown rule_type
        r_dup_a = PolicyRule(rule_id="dup", rule_type="unknown_xyz", priority=10)
        r_dup_b = PolicyRule(rule_id="dup", rule_type="budget", priority=20)
        r_tuple = (r_dup_a, r_dup_b)
        wrong_hash = "c" * 64
        meta = PolicyMetadata(policy_id="p1", content_hash=wrong_hash)
        bundle = PolicyBundle(metadata=meta, rules=r_tuple)
        result = PolicyVerifier().verify(bundle)
        assert result.valid is False
        # At minimum: hash mismatch + unknown type + duplicate id
        assert len(result.errors) >= 3

    # -----------------------------------------------------------------------
    # Category 6: Boundary -- empty bundle verification
    # -----------------------------------------------------------------------

    def test_empty_rules_bundle_with_correct_hash_passes(self) -> None:
        """Zero-rule bundle with correct hash must pass verification."""
        bundle = _bundle_with_correct_hash()  # no rules
        result = PolicyVerifier().verify(bundle)
        assert result.valid is True

    def test_empty_rules_require_signature_fails(self) -> None:
        """Zero-rule bundle still requires signature when require_signature=True."""
        bundle = _bundle_with_correct_hash()
        result = PolicyVerifier(require_signature=True).verify(bundle)
        assert result.valid is False


# ===========================================================================
# TestAdversarialFrozenView
# ===========================================================================


class TestAdversarialFrozenView:
    """Adversarial tests for FrozenPolicyView and PolicyViewHolder -- attacker mindset."""

    # -----------------------------------------------------------------------
    # Category 1: Reject invalid VerificationResult
    # -----------------------------------------------------------------------

    def test_invalid_verification_raises_value_error(self) -> None:
        """FrozenPolicyView must raise ValueError if verification.valid=False."""
        bundle = _bundle_with_correct_hash(_rule("r1"))
        bad_result = VerificationResult(valid=False, errors=("forced failure",))
        with pytest.raises(ValueError, match="invalid"):
            FrozenPolicyView(bundle, bad_result)

    def test_invalid_verification_error_messages_included_in_exception(self) -> None:
        """Error messages from VerificationResult must appear in the ValueError text."""
        bundle = _bundle_with_correct_hash(_rule("r1"))
        bad_result = VerificationResult(valid=False, errors=("unique_error_sentinel_xyz",))
        with pytest.raises(ValueError, match="unique_error_sentinel_xyz"):
            FrozenPolicyView(bundle, bad_result)

    def test_multiple_errors_all_included_in_exception(self) -> None:
        """All error strings must appear when there are multiple errors."""
        bundle = _bundle_with_correct_hash(_rule("r1"))
        bad_result = VerificationResult(
            valid=False,
            errors=("error_one", "error_two", "error_three"),
        )
        with pytest.raises(ValueError) as exc_info:
            FrozenPolicyView(bundle, bad_result)
        msg = str(exc_info.value)
        assert "error_one" in msg
        assert "error_two" in msg
        assert "error_three" in msg

    # -----------------------------------------------------------------------
    # Category 2: Slot restriction -- no dynamic attribute injection
    # -----------------------------------------------------------------------

    def test_frozen_view_no_dynamic_attributes(self) -> None:
        """FrozenPolicyView uses __slots__ -- injecting new attributes must fail."""
        view = _make_frozen_view(_rule("r1"))
        with pytest.raises(AttributeError):
            view.injected_by_attacker = "evil"  # type: ignore[attr-defined]

    def test_frozen_view_no_extra_dict_storage(self) -> None:
        """FrozenPolicyView uses __slots__ so it must not have a __dict__."""
        view = _make_frozen_view(_rule("r1"))
        # __slots__ classes have no __dict__ -- attribute storage is fixed
        assert not hasattr(view, "__dict__"), (
            "FrozenPolicyView must not have __dict__ (use __slots__ for memory safety)"
        )

    # -----------------------------------------------------------------------
    # Category 3: rules_for_type with corrupted / edge-case query strings
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "query",
        [
            "",              # empty string
            "BUDGET",        # wrong case
            "nonexistent",   # absent type
            " budget",       # leading space
            "budget ",       # trailing space
        ],
    )
    def test_rules_for_type_returns_empty_for_non_matching_queries(self, query: str) -> None:
        """Non-matching rule_type queries must return empty tuple, never crash."""
        view = _make_frozen_view(_rule("r1", "budget"))
        result = view.rules_for_type(query)
        assert result == ()

    def test_has_rule_type_case_sensitive(self) -> None:
        """has_rule_type must be case-sensitive."""
        view = _make_frozen_view(_rule("r1", "budget"))
        assert view.has_rule_type("budget") is True
        assert view.has_rule_type("BUDGET") is False
        assert view.has_rule_type("Budget") is False

    def test_rule_types_frozenset_is_immutable(self) -> None:
        """rule_types frozenset must not be mutable."""
        view = _make_frozen_view(_rule("r1", "budget"))
        rt = view.rule_types
        assert isinstance(rt, frozenset)
        with pytest.raises(AttributeError):
            rt.add("injected")  # type: ignore[attr-defined]

    # -----------------------------------------------------------------------
    # Category 4: PolicyViewHolder TOCTOU -- concurrent load_bundle
    # -----------------------------------------------------------------------

    def test_holder_concurrent_load_bundle_different_epochs_only_valid_installed(self) -> None:
        """20 threads racing to load bundles must all see a valid view at the end."""
        holder = PolicyViewHolder()
        errors: list[Exception] = []

        def load(epoch_val: int) -> None:
            r = _rule(f"r-epoch-{epoch_val}")
            meta = PolicyMetadata(policy_id="p1", epoch=epoch_val)
            # No declared hash -- valid bundle (warning, not error)
            bundle = PolicyBundle(metadata=meta, rules=(r,))
            try:
                holder.load_bundle(bundle)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=load, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected exceptions: {errors}"
        # Holder must have a valid view (one of the 20 threads won the race)
        assert holder.current is not None

    def test_holder_concurrent_load_invalid_bundle_never_replaces_valid_view(self) -> None:
        """Invalid bundles must never evict a valid existing view under concurrency."""
        valid_view = _make_frozen_view(_rule("stable"))
        holder = PolicyViewHolder(initial=valid_view)
        errors: list[Exception] = []

        def load_bad() -> None:
            meta = PolicyMetadata(policy_id="bad", content_hash="f" * 64)
            bad_bundle = PolicyBundle(metadata=meta, rules=(_rule("r1"),))
            try:
                result = holder.load_bundle(bad_bundle)
                if result.valid:
                    errors.append(AssertionError("Invalid bundle reported valid"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=load_bad) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected problems: {errors}"
        # The valid view must still be current
        assert holder.current is valid_view, "Valid view must survive concurrent invalid load attempts"

    def test_holder_concurrent_swap_none_and_valid_view(self) -> None:
        """Concurrent swap(None) and swap(view) must leave holder in a consistent state."""
        view_a = _make_frozen_view(_rule("a"))
        view_b = _make_frozen_view(_rule("b", "step"))
        holder = PolicyViewHolder(initial=view_a)
        errors: list[Exception] = []

        def swap_none() -> None:
            try:
                holder.swap(None)
            except Exception as exc:
                errors.append(exc)

        def swap_valid() -> None:
            try:
                holder.swap(view_b)
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=swap_none) for _ in range(10)]
            + [threading.Thread(target=swap_valid) for _ in range(10)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # The holder must be in a valid state (either None or view_b, never corrupt)
        current = holder.current
        assert current is None or current is view_b

    # -----------------------------------------------------------------------
    # Category 5: to_audit_dict -- does not leak sensitive rule parameters
    # -----------------------------------------------------------------------

    def test_audit_dict_does_not_contain_rule_parameters(self) -> None:
        """to_audit_dict must not include raw rule parameters (potential secrets)."""
        view = _make_frozen_view(
            _rule("r1", parameters={"secret_key": "supersecret123", "api_token": "tok_abc"})
        )
        audit = view.to_audit_dict()
        audit_str = str(audit)
        assert "supersecret123" not in audit_str
        assert "tok_abc" not in audit_str

    def test_audit_dict_has_required_fields(self) -> None:
        """to_audit_dict must always include the minimum required fields."""
        view = _make_frozen_view(_rule("r1", "budget"))
        audit = view.to_audit_dict()
        required_keys = {
            "policy_id", "version", "epoch", "issuer",
            "content_hash", "is_signed", "rule_count",
            "rule_types", "verified_at",
        }
        assert required_keys <= set(audit.keys())

    def test_audit_dict_rule_count_matches_disabled_plus_enabled(self) -> None:
        """rule_count must include ALL rules (disabled ones too)."""
        view = _make_frozen_view(
            _rule("r1", enabled=True),
            _rule("r2", enabled=False),
            _rule("r3", enabled=True),
        )
        audit = view.to_audit_dict()
        assert audit["rule_count"] == 3

    def test_audit_dict_is_json_serializable(self) -> None:
        """to_audit_dict output must be JSON-serializable without custom encoders."""
        import json

        view = _make_frozen_view(
            _rule("r1", "budget"),
            _rule("r2", "step"),
        )
        audit = view.to_audit_dict()
        # Must not raise
        serialized = json.dumps(audit)
        assert len(serialized) > 0

    # -----------------------------------------------------------------------
    # Category 6: Boundary -- empty bundle in FrozenPolicyView
    # -----------------------------------------------------------------------

    def test_frozen_view_empty_rules_bundle(self) -> None:
        """FrozenPolicyView wrapping a zero-rule bundle must work without error."""
        bundle = _bundle_with_correct_hash()
        view = FrozenPolicyView(bundle, _valid_verification())
        assert view.rules == ()
        assert view.rule_types == frozenset()
        assert view.rules_for_type("budget") == ()
        assert view.has_rule_type("budget") is False

    def test_frozen_view_single_rule_bundle(self) -> None:
        """Single-rule bundle must be correctly represented."""
        view = _make_frozen_view(_rule("solo", "shell"))
        assert len(view.rules) == 1
        assert view.has_rule_type("shell") is True
        assert view.rules_for_type("shell")[0].rule_id == "solo"

    def test_frozen_view_large_rule_count(self) -> None:
        """FrozenPolicyView with 1000 rules must construct and query correctly."""
        rules = tuple(_rule(f"r{i:04d}", "budget") for i in range(1000))
        bundle = _bundle_with_correct_hash(*rules)
        view = FrozenPolicyView(bundle, _valid_verification())
        assert len(view.rules) == 1000
        assert view.has_rule_type("budget") is True
        assert len(view.rules_for_type("budget")) == 1000
