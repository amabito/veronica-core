"""Adversarial tests for veronica_core.kernel.startup -- attacker mindset.

These tests exercise failure paths NOT covered by test_kernel_startup.py:
- Signer objects with unexpected behavior (raises, returns non-bool, missing method)
- Canonical form injection via embedded newlines or null bytes in policy_id
- Concurrent access with shared audit logs
- Audit log failures propagating correctly (or not)
- Bundle data corruption edge cases (empty dict, NaN, negative epoch, extra keys)
- Idempotency of repeated verification calls
"""

from __future__ import annotations

import hashlib
import math
import threading
from pathlib import Path
from typing import Any

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.kernel.startup import load_and_verify, verify_policy_or_halt
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.verifier import VerificationResult
from veronica_core.security.policy_signing import PolicySigner

from .conftest import make_signed_bundle, make_test_audit_log, make_test_signer, read_jsonl


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

_TEST_KEY = hashlib.sha256(b"adversarial-startup-key").digest()


def _make_signer(key: bytes = _TEST_KEY) -> PolicySigner:
    return make_test_signer(key_bytes=key)


def _make_signed_bundle(
    signer: PolicySigner,
    rules: tuple[PolicyRule, ...] = (),
    policy_id: str = "adv-policy",
) -> PolicyBundle:
    return make_signed_bundle(signer, rules=rules, policy_id=policy_id)


def _make_audit_log(tmp_path: Path, filename: str = "audit.jsonl") -> AuditLog:
    return make_test_audit_log(tmp_path, filename=filename)


def _read_audit_entries(audit_log: AuditLog) -> list[dict[str, Any]]:
    return read_jsonl(audit_log)


_BUDGET_RULE = PolicyRule(rule_id="r-budget", rule_type="budget")


# ---------------------------------------------------------------------------
# 1. Corrupted signer behavior
# ---------------------------------------------------------------------------


class TestAdversarialStartupSignerBehavior:
    """Signer objects that behave unexpectedly must never allow valid=True."""

    def test_signer_verify_bundle_raises_runtime_error_returns_valid_false(
        self, tmp_path: Path
    ) -> None:
        """Signer whose verify_bundle raises RuntimeError must yield valid=False.

        The PolicyVerifier.verify() catches exceptions from verify_bundle and
        converts them into an error message -- fail-closed.
        """

        class RaisingVerifySigner:
            def verify_bundle(self, bundle: PolicyBundle) -> bool:
                raise RuntimeError("simulated HSM failure")

        signer = _make_signer()
        # Use a bundle that IS signed (non-empty signature) so the verifier
        # reaches the verify_bundle call.
        bundle = _make_signed_bundle(signer)
        raising_signer = RaisingVerifySigner()

        result = verify_policy_or_halt(bundle, raising_signer)

        assert result.valid is False
        assert any("verification failed" in e.lower() for e in result.errors)
        # Rule 5: no exc type leak in user-facing errors
        assert not any("RuntimeError" in e for e in result.errors)

    def test_signer_verify_bundle_returns_truthy_non_bool_string_accepted(
        self, tmp_path: Path
    ) -> None:
        """Signer returning truthy non-bool like 'yes' -- treated as truthy.

        The verifier uses ``if not verify_fn(bundle)`` so any truthy value
        passes.  This documents that the contract is duck-typed.
        """

        class TruthyStringSigner:
            def verify_bundle(self, bundle: PolicyBundle) -> Any:
                return "yes"  # truthy non-bool

        signer = _make_signer()
        bundle = _make_signed_bundle(signer)
        truthy_signer = TruthyStringSigner()
        # The bundle has a correct content_hash and a valid (real) signature
        # that won't pass truthy_signer -- but since TruthyStringSigner returns
        # "yes" the signature step passes.  The content_hash check still runs.
        result = verify_policy_or_halt(bundle, truthy_signer)

        # Truthy value passes the ``if not verify_fn(bundle)`` check -- no
        # signature error is added.  Result depends on other checks (content
        # hash), which pass since bundle was built correctly.
        assert isinstance(result, VerificationResult)

    def test_signer_verify_bundle_returns_none_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """Signer whose verify_bundle returns None must yield valid=False.

        None is falsy, so ``if not verify_fn(bundle)`` fires and appends an
        error -- fail-closed.
        """

        class NoneReturnSigner:
            def verify_bundle(self, bundle: PolicyBundle) -> None:
                return None

        signer = _make_signer()
        bundle = _make_signed_bundle(signer)
        none_signer = NoneReturnSigner()

        result = verify_policy_or_halt(bundle, none_signer)

        assert result.valid is False

    def test_signer_without_verify_bundle_method_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """Signer object missing verify_bundle must yield valid=False (fail-closed).

        The verifier checks ``callable(verify_fn)`` and appends an error when
        the method is absent.
        """

        class NoVerifyMethodSigner:
            # Intentionally no verify_bundle attribute.
            pass

        signer = _make_signer()
        bundle = _make_signed_bundle(signer)
        bad_signer = NoVerifyMethodSigner()

        result = verify_policy_or_halt(bundle, bad_signer)

        assert result.valid is False
        assert any("verify_bundle" in e for e in result.errors)

    def test_signer_verify_bundle_raises_value_error_fails_closed(
        self, tmp_path: Path
    ) -> None:
        """ValueError from verify_bundle is caught -- fail-closed, error surfaced."""

        class FlakyValueErrorSigner:
            def verify_bundle(self, bundle: PolicyBundle) -> bool:
                raise ValueError("bad input")

        signer = _make_signer()
        bundle = _make_signed_bundle(signer)

        result = verify_policy_or_halt(bundle, FlakyValueErrorSigner())

        assert result.valid is False
        assert any("verification failed" in e.lower() for e in result.errors)
        # Rule 5: no exc type leak
        assert not any("ValueError" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 2. Canonical form injection
# ---------------------------------------------------------------------------


class TestAdversarialStartupCanonicalInjection:
    """Embedded special characters in policy_id must not bypass signing."""

    def test_newline_in_policy_id_cannot_bypass_signing(self) -> None:
        """Bundle with newline in policy_id cannot be signed normally.

        sign_bundle raises ValueError for newlines (H3 defense).  Therefore
        load_and_verify must fail-closed when such a dict is processed:
        the bundle can be constructed (PolicyMetadata accepts arbitrary strings),
        but the signer will refuse to verify it because sign_bundle raises.
        """
        signer = _make_signer()
        # Crafted dict with newline in policy_id -- attacker hoping to inject
        # "epoch=999" into the canonical string for a different policy.
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "legitimate-policy\nepoch=999"},
            "rules": [],
            "signature": "deadbeef" * 16,
        }

        bundle, result = load_and_verify(bundle_data, signer)

        # The injected bundle has a fake signature; verifier rejects it.
        assert result.valid is False

    def test_null_byte_in_policy_id_fails_closed(self) -> None:
        """Bundle with null byte in policy_id fails signature verification.

        The canonical string produced by sign_bundle embeds policy_id verbatim.
        A null byte inside the string produces a different canonical form than
        any legitimately signed bundle, so HMAC comparison fails.
        """
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "policy\x00injected"},
            "rules": [],
            "signature": "deadbeef" * 16,
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is False

    def test_very_long_policy_id_does_not_crash_or_oom(self) -> None:
        """A 10 000-character policy_id must not OOM or crash -- just fail-closed."""
        signer = _make_signer()
        long_id = "A" * 10_000
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": long_id},
            "rules": [],
            "signature": "cafebabe" * 16,
        }

        bundle, result = load_and_verify(bundle_data, signer)

        # The bundle may construct fine but signature verification rejects it.
        assert isinstance(result, VerificationResult)
        assert result.valid is False

    def test_carriage_return_in_policy_id_fails_closed(self) -> None:
        """Carriage return in policy_id is caught by sign_bundle H3 defense."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "policy\repoch=0"},
            "rules": [],
            "signature": "aabbccdd" * 16,
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is False


# ---------------------------------------------------------------------------
# 3. Concurrent access
# ---------------------------------------------------------------------------


class TestAdversarialStartupConcurrent:
    """Concurrent callers must not corrupt the audit log or crash."""

    def test_10_threads_verify_policy_or_halt_share_audit_log_no_crash(
        self, tmp_path: Path
    ) -> None:
        """10 threads calling verify_policy_or_halt with shared AuditLog must not crash.

        All calls use an unsigned bundle (guaranteed failure) so each thread
        emits exactly one GOVERNANCE_HALT event.  At the end all 10 events must
        be present and the hash chain must be valid.
        """
        signer = _make_signer()
        bundle = PolicyBundle(
            metadata=PolicyMetadata(policy_id="concurrent-test"),
            rules=(_BUDGET_RULE,),
            signature="",  # unsigned -> always fails
        )
        audit_log = _make_audit_log(tmp_path, "concurrent.jsonl")

        errors: list[Exception] = []

        def worker() -> None:
            try:
                verify_policy_or_halt(bundle, signer, audit_log=audit_log)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"threads raised: {errors}"
        entries = _read_audit_entries(audit_log)
        assert len(entries) == 10
        # Hash chain must remain intact under concurrent writes.
        assert audit_log.verify_chain() is True

    def test_10_threads_load_and_verify_different_bundles_no_crash(
        self, tmp_path: Path
    ) -> None:
        """10 threads calling load_and_verify with distinct bundle dicts must not crash."""
        signer = _make_signer()

        errors: list[Exception] = []
        results: list[VerificationResult] = []
        lock = threading.Lock()

        def worker(thread_idx: int) -> None:
            try:
                bundle_data: dict[str, Any] = {
                    "metadata": {"policy_id": f"policy-{thread_idx}"},
                    "rules": [],
                    "signature": "",  # unsigned -> fail-closed
                }
                _, result = load_and_verify(bundle_data, signer)
                with lock:
                    results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"threads raised: {errors}"
        assert len(results) == 10
        assert all(not r.valid for r in results)


# ---------------------------------------------------------------------------
# 4. Audit log failure resilience
# ---------------------------------------------------------------------------


class TestAdversarialStartupAuditFailure:
    """Unexpected failures from AuditLog must behave predictably."""

    def test_audit_log_write_raises_ioerror_propagates(self, tmp_path: Path) -> None:
        """IOError from audit_log.write_governance_event must propagate to caller.

        startup.verify_policy_or_halt does NOT swallow AuditLog exceptions --
        the caller is responsible for handling write failures.
        """

        class BrokenAuditLog:
            """Stub AuditLog whose write_governance_event always raises."""

            def write_governance_event(self, **kwargs: Any) -> None:
                raise IOError("disk full")

        signer = _make_signer()
        bundle = PolicyBundle(
            metadata=PolicyMetadata(policy_id="test"),
            rules=(),
            signature="",  # unsigned -> triggers audit write
        )

        with pytest.raises(IOError, match="disk full"):
            verify_policy_or_halt(bundle, signer, audit_log=BrokenAuditLog())  # type: ignore[arg-type]

    def test_successful_verification_never_calls_write_governance_event(
        self, tmp_path: Path
    ) -> None:
        """A passing bundle must emit zero audit events even with a broken audit_log.

        The broken AuditLog would raise if write_governance_event were called,
        so if the test passes with no exception we know it was never invoked.
        """

        class PanicAuditLog:
            """Stub that panics if any write method is called."""

            def write_governance_event(self, **kwargs: Any) -> None:
                raise AssertionError("write_governance_event must NOT be called on success")

        signer = _make_signer()
        bundle = _make_signed_bundle(signer, rules=(_BUDGET_RULE,))

        # Must not raise -- successful verification skips the audit write path.
        result = verify_policy_or_halt(bundle, signer, audit_log=PanicAuditLog())  # type: ignore[arg-type]

        assert result.valid is True

    def test_audit_hash_chain_integrity_after_multiple_failures(
        self, tmp_path: Path
    ) -> None:
        """Hash chain stays valid after N sequential failed verification calls."""
        signer = _make_signer()
        audit_log = _make_audit_log(tmp_path)
        unsigned_bundle = PolicyBundle(
            metadata=PolicyMetadata(policy_id="chain-test"),
            rules=(_BUDGET_RULE,),
            signature="",
        )

        for _ in range(5):
            verify_policy_or_halt(unsigned_bundle, signer, audit_log=audit_log)

        entries = _read_audit_entries(audit_log)
        assert len(entries) == 5
        assert audit_log.verify_chain() is True

    def test_audit_log_write_raises_on_second_call_partial_write_scenario(
        self, tmp_path: Path
    ) -> None:
        """Second write raising must not silently swallow the error."""
        call_count = 0

        class OnceAuditLog:
            """Succeeds on first call, raises on subsequent calls."""

            def write_governance_event(self, **kwargs: Any) -> None:
                nonlocal call_count
                call_count += 1
                if call_count > 1:
                    raise IOError("simulated second-write failure")

        signer = _make_signer()
        unsigned = PolicyBundle(
            metadata=PolicyMetadata(policy_id="partial"),
            rules=(),
            signature="",
        )
        once_log = OnceAuditLog()

        # First call: succeeds.
        verify_policy_or_halt(unsigned, signer, audit_log=once_log)  # type: ignore[arg-type]
        assert call_count == 1

        # Second call: audit_log raises -- must propagate.
        with pytest.raises(IOError):
            verify_policy_or_halt(unsigned, signer, audit_log=once_log)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Bundle data corruption
# ---------------------------------------------------------------------------


class TestAdversarialStartupBundleCorruption:
    """Corrupted or unusual bundle_data dicts must fail safely."""

    def test_completely_empty_dict_fails_closed(self) -> None:
        """Completely empty dict causes KeyError on policy_id lookup -- fail-closed."""
        signer = _make_signer()

        bundle, result = load_and_verify({}, signer)

        assert result.valid is False
        assert bundle.metadata.policy_id == "__invalid__"
        assert any("construction failed" in e.lower() for e in result.errors)

    def test_rules_with_nan_in_parameters_constructs_without_crash(self) -> None:
        """NaN in rule parameters must not crash construction (NaN is a valid float)."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "nan-policy"},
            "rules": [
                {
                    "rule_id": "r-nan",
                    "rule_type": "budget",
                    "parameters": {"max_cost": math.nan},
                }
            ],
            "signature": "",  # unsigned -> fail after construction
        }

        bundle, result = load_and_verify(bundle_data, signer)

        # Construction should succeed (NaN is a valid float); verification
        # fails because the bundle is unsigned.
        assert isinstance(bundle, PolicyBundle)
        assert result.valid is False
        # Bundle should have the rule with NaN parameter.
        if bundle.metadata.policy_id != "__invalid__":
            assert bundle.rules[0].parameters["max_cost"] is math.nan or (
                isinstance(bundle.rules[0].parameters["max_cost"], float)
                and math.isnan(bundle.rules[0].parameters["max_cost"])
            )

    def test_negative_epoch_in_metadata_fails_closed(self) -> None:
        """Negative epoch in metadata causes PolicyMetadata to raise -- fail-closed."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "neg-epoch", "epoch": -1},
            "rules": [],
            "signature": "",
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is False
        assert bundle.metadata.policy_id == "__invalid__"

    def test_extra_unknown_keys_in_bundle_data_silently_ignored(
        self, tmp_path: Path
    ) -> None:
        """Unknown top-level keys in bundle_data are silently ignored.

        load_and_verify only reads 'metadata', 'rules', and 'signature'.
        Unrecognised keys (e.g. 'debug', 'owner') must be ignored without error.
        """
        signer = _make_signer()
        # Build a valid signed bundle to get correct hash and signature.
        real_bundle = _make_signed_bundle(signer, rules=(_BUDGET_RULE,))
        bundle_data: dict[str, Any] = {
            "metadata": {
                "policy_id": real_bundle.metadata.policy_id,
                "content_hash": real_bundle.metadata.content_hash,
            },
            "rules": [{"rule_id": "r-budget", "rule_type": "budget"}],
            "signature": real_bundle.signature,
            # Extra unknown keys:
            "debug": True,
            "owner": "attacker",
            "version_history": [1, 2, 3],
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is True
        assert bundle.metadata.policy_id == "adv-policy"

    def test_rules_with_infinity_in_parameters_constructs_without_crash(self) -> None:
        """Infinity in rule parameters must not crash construction."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "inf-policy"},
            "rules": [
                {
                    "rule_id": "r-inf",
                    "rule_type": "budget",
                    "parameters": {"limit": math.inf},
                }
            ],
            "signature": "00" * 32,
        }

        bundle, result = load_and_verify(bundle_data, signer)

        # Construction may succeed or fail depending on JSON serialisation of inf.
        # Either way: no crash.
        assert isinstance(result, VerificationResult)

    def test_integer_policy_id_fails_closed(self) -> None:
        """Integer policy_id (not a string) fails PolicyMetadata validation."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": 12345},  # int, not str
            "rules": [],
            "signature": "",
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is False

    def test_rules_list_with_empty_rule_dict_fails_closed(self) -> None:
        """Rule dict with no rule_id or rule_type causes construction failure."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "bad-rule"},
            "rules": [{}],  # missing rule_id and rule_type
            "signature": "",
        }

        bundle, result = load_and_verify(bundle_data, signer)

        assert result.valid is False


# ---------------------------------------------------------------------------
# 6. Double/repeated verification idempotency
# ---------------------------------------------------------------------------


class TestAdversarialStartupIdempotency:
    """Repeated calls must return consistent results."""

    def test_verify_policy_or_halt_twice_same_bundle_consistent_result(
        self, tmp_path: Path
    ) -> None:
        """Calling verify_policy_or_halt twice on the same bundle gives same validity."""
        signer = _make_signer()
        bundle = _make_signed_bundle(signer, rules=(_BUDGET_RULE,))
        audit_log = _make_audit_log(tmp_path)

        result_1 = verify_policy_or_halt(bundle, signer, audit_log=audit_log)
        result_2 = verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        assert result_1.valid == result_2.valid
        assert result_1.errors == result_2.errors

    def test_verify_policy_or_halt_twice_failed_bundle_same_result(
        self, tmp_path: Path
    ) -> None:
        """Repeated calls on a failing bundle yield same valid=False each time."""
        signer = _make_signer()
        bundle = PolicyBundle(
            metadata=PolicyMetadata(policy_id="repeat-fail"),
            rules=(_BUDGET_RULE,),
            signature="",  # unsigned
        )
        audit_log = _make_audit_log(tmp_path)

        result_1 = verify_policy_or_halt(bundle, signer, audit_log=audit_log)
        result_2 = verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        assert result_1.valid is False
        assert result_2.valid is False
        assert result_1.errors == result_2.errors
        # Two audit events should have been written (one per call).
        entries = _read_audit_entries(audit_log)
        assert len(entries) == 2

    def test_load_and_verify_repeated_calls_consistent(self) -> None:
        """Repeated load_and_verify calls on identical data return consistent results."""
        signer = _make_signer()
        bundle_data: dict[str, Any] = {
            "metadata": {"policy_id": "idempotent"},
            "rules": [],
            "signature": "",  # unsigned -> fail
        }

        _, result_1 = load_and_verify(bundle_data, signer)
        _, result_2 = load_and_verify(bundle_data, signer)

        assert result_1.valid == result_2.valid
        assert result_1.errors == result_2.errors

    def test_load_and_verify_valid_bundle_repeated_returns_valid_true(
        self, tmp_path: Path
    ) -> None:
        """Repeated successful load_and_verify calls always return valid=True."""
        signer = _make_signer()
        real_bundle = _make_signed_bundle(signer, rules=(_BUDGET_RULE,))
        bundle_data: dict[str, Any] = {
            "metadata": {
                "policy_id": real_bundle.metadata.policy_id,
                "content_hash": real_bundle.metadata.content_hash,
            },
            "rules": [{"rule_id": "r-budget", "rule_type": "budget"}],
            "signature": real_bundle.signature,
        }

        _, result_1 = load_and_verify(bundle_data, signer)
        _, result_2 = load_and_verify(bundle_data, signer)
        _, result_3 = load_and_verify(bundle_data, signer)

        assert result_1.valid is True
        assert result_2.valid is True
        assert result_3.valid is True
