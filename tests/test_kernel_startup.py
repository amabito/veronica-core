"""Tests for veronica_core.kernel.startup -- startup guard wiring.

Coverage:
- verify_policy_or_halt with valid signed bundle -> valid=True
- verify_policy_or_halt with unsigned bundle -> valid=False, audit event emitted
- verify_policy_or_halt with bad content hash -> valid=False
- verify_policy_or_halt without audit_log -> no crash
- load_and_verify with valid data
- load_and_verify with invalid data (exception during construction) -> fail-closed
- Adversarial: signer=None, corrupted bundle, empty rules, None audit_log
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from veronica_core.audit.log import AuditLog
from veronica_core.kernel.startup import load_and_verify, verify_policy_or_halt
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.security.policy_signing import PolicySigner

from .conftest import make_signed_bundle, make_test_audit_log, make_test_bundle, make_test_signer, read_jsonl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_KEY = hashlib.sha256(b"startup-test-key").digest()


def _make_signer(key: bytes = _TEST_KEY) -> PolicySigner:
    return make_test_signer(key_bytes=key)


def _make_bundle(
    rules: tuple[PolicyRule, ...] = (),
    signature: str = "",
    with_content_hash: bool = True,
    policy_id: str = "test-policy",
) -> PolicyBundle:
    return make_test_bundle(
        rules=rules,
        signature=signature,
        with_content_hash=with_content_hash,
        policy_id=policy_id,
    )


def _signed_bundle(
    signer: PolicySigner,
    rules: tuple[PolicyRule, ...] = (),
) -> PolicyBundle:
    return make_signed_bundle(signer, rules=rules)


def _make_audit_log(tmp_path: Path) -> AuditLog:
    return make_test_audit_log(tmp_path)


def _read_audit_events(audit_log: AuditLog) -> list[dict[str, Any]]:
    return read_jsonl(audit_log)


_RULE = PolicyRule(rule_id="r1", rule_type="budget")


# ---------------------------------------------------------------------------
# verify_policy_or_halt -- valid bundle
# ---------------------------------------------------------------------------


class TestVerifyPolicyOrHalt:
    def test_valid_signed_bundle_returns_valid_true(self, tmp_path: Path) -> None:
        """A properly signed bundle passes verification."""
        signer = _make_signer()
        bundle = _signed_bundle(signer, rules=(_RULE,))
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        assert result.valid is True
        assert result.errors == ()

    def test_valid_bundle_no_audit_event_emitted(self, tmp_path: Path) -> None:
        """No audit event is written when verification succeeds."""
        signer = _make_signer()
        bundle = _signed_bundle(signer, rules=(_RULE,))
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        assert events == []

    # ------------------------------------------------------------------
    # Unsigned bundle
    # ------------------------------------------------------------------

    def test_unsigned_bundle_returns_valid_false(self, tmp_path: Path) -> None:
        """An unsigned bundle fails verification (require_signature=True)."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        assert result.valid is False

    def test_unsigned_bundle_emits_halt_event(self, tmp_path: Path) -> None:
        """A GOVERNANCE_HALT audit event is emitted for an unsigned bundle."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        assert len(events) == 1
        data = events[0]["data"]
        assert events[0]["event_type"] == "GOVERNANCE_HALT"
        assert data["decision"] == "HALT"

    def test_unsigned_bundle_audit_event_has_policy_unsigned_reason_code(
        self, tmp_path: Path
    ) -> None:
        """The reason_code for an unsigned bundle is POLICY_UNSIGNED."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        data = events[0]["data"]
        assert data["reason_code"] == "POLICY_UNSIGNED"

    # ------------------------------------------------------------------
    # Bad content hash
    # ------------------------------------------------------------------

    def test_bad_content_hash_returns_valid_false(self, tmp_path: Path) -> None:
        """A bundle whose declared content_hash mismatches the computed hash fails.

        Strategy: sign a bundle without a content_hash (so sign_bundle succeeds),
        then swap in a wrong content_hash metadata.  The verifier detects the
        mismatch independently of the signing step.
        """
        signer = _make_signer()
        # Sign a bundle that has no declared content_hash (sign_bundle accepts it).
        unsigned_no_hash = PolicyBundle(
            metadata=PolicyMetadata(policy_id="test-policy", content_hash=""),
            rules=(_RULE,),
        )
        sig = signer.sign_bundle(unsigned_no_hash)
        # Now build the final bundle with a wrong content_hash but the valid signature.
        bad_meta = PolicyMetadata(
            policy_id="test-policy",
            content_hash="deadbeef" * 8,  # 64 hex chars of garbage
        )
        bundle = PolicyBundle(metadata=bad_meta, rules=(_RULE,), signature=sig)
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        assert result.valid is False

    def test_bad_content_hash_emits_audit_event(self, tmp_path: Path) -> None:
        """A GOVERNANCE_HALT event is emitted when content hash is wrong."""
        signer = _make_signer()
        unsigned_no_hash = PolicyBundle(
            metadata=PolicyMetadata(policy_id="test-policy", content_hash=""),
            rules=(_RULE,),
        )
        sig = signer.sign_bundle(unsigned_no_hash)
        bad_meta = PolicyMetadata(
            policy_id="test-policy",
            content_hash="deadbeef" * 8,
        )
        bundle = PolicyBundle(metadata=bad_meta, rules=(_RULE,), signature=sig)
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        assert len(events) == 1
        assert events[0]["event_type"] == "GOVERNANCE_HALT"

    # ------------------------------------------------------------------
    # No audit_log
    # ------------------------------------------------------------------

    def test_no_audit_log_does_not_crash_on_failure(self) -> None:
        """Passing audit_log=None must not raise on verification failure."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")

        result = verify_policy_or_halt(bundle, signer, audit_log=None)

        assert result.valid is False

    def test_no_audit_log_does_not_crash_on_success(self) -> None:
        """Passing audit_log=None must not raise on successful verification."""
        signer = _make_signer()
        bundle = _signed_bundle(signer, rules=(_RULE,))

        result = verify_policy_or_halt(bundle, signer, audit_log=None)

        assert result.valid is True


# ---------------------------------------------------------------------------
# load_and_verify -- valid data
# ---------------------------------------------------------------------------


class TestLoadAndVerify:
    def _bundle_dict(
        self,
        signer: PolicySigner,
        rules: list[dict] | None = None,
        sign: bool = True,
    ) -> dict[str, Any]:
        """Build a raw bundle dict, optionally signed."""
        if rules is None:
            rules = [{"rule_id": "r1", "rule_type": "budget"}]

        # Build actual bundle to get correct content_hash and signature.
        rule_objects = tuple(
            PolicyRule(
                rule_id=r["rule_id"],
                rule_type=r["rule_type"],
                parameters=r.get("parameters", {}),
                enabled=r.get("enabled", True),
                priority=r.get("priority", 100),
            )
            for r in rules
        )
        tmp = PolicyBundle(
            metadata=PolicyMetadata(policy_id="test-policy"),
            rules=rule_objects,
        )
        content_hash = tmp.content_hash()
        unsigned = PolicyBundle(
            metadata=PolicyMetadata(
                policy_id="test-policy",
                content_hash=content_hash,
            ),
            rules=rule_objects,
        )
        signature = signer.sign_bundle(unsigned) if sign else ""

        return {
            "metadata": {
                "policy_id": "test-policy",
                "version": "1.0.0",
                "epoch": 0,
                "content_hash": content_hash,
            },
            "rules": rules,
            "signature": signature,
        }

    def test_valid_data_returns_valid_true(self, tmp_path: Path) -> None:
        """Valid signed bundle dict produces valid=True result."""
        signer = _make_signer()
        data = self._bundle_dict(signer)
        audit_log = _make_audit_log(tmp_path)

        bundle, result = load_and_verify(data, signer, audit_log=audit_log)

        assert result.valid is True
        assert isinstance(bundle, PolicyBundle)

    def test_valid_data_bundle_has_correct_policy_id(self) -> None:
        """The returned bundle has the policy_id from the input dict."""
        signer = _make_signer()
        data = self._bundle_dict(signer)

        bundle, _ = load_and_verify(data, signer)

        assert bundle.metadata.policy_id == "test-policy"

    def test_valid_data_bundle_has_correct_rule(self) -> None:
        """The returned bundle contains the rules from the input dict."""
        signer = _make_signer()
        data = self._bundle_dict(
            signer, rules=[{"rule_id": "r-budget", "rule_type": "budget"}]
        )

        bundle, result = load_and_verify(data, signer)

        assert result.valid is True
        assert len(bundle.rules) == 1
        assert bundle.rules[0].rule_id == "r-budget"

    # ------------------------------------------------------------------
    # Construction failure (fail-closed)
    # ------------------------------------------------------------------

    def test_missing_policy_id_returns_valid_false(self) -> None:
        """Missing policy_id in metadata causes fail-closed result."""
        signer = _make_signer()
        bad_data: dict[str, Any] = {
            "metadata": {},  # policy_id missing -> PolicyMetadata raises
            "rules": [],
        }

        bundle, result = load_and_verify(bad_data, signer)

        assert result.valid is False
        assert len(result.errors) == 1
        assert "construction failed" in result.errors[0].lower()

    def test_invalid_data_returns_placeholder_bundle(self) -> None:
        """On construction failure the placeholder bundle has policy_id='__invalid__'."""
        signer = _make_signer()
        bad_data: dict[str, Any] = {"metadata": {}, "rules": []}

        bundle, result = load_and_verify(bad_data, signer)

        assert result.valid is False
        assert bundle.metadata.policy_id == "__invalid__"

    def test_rule_missing_rule_type_fails_closed(self) -> None:
        """A rule dict without rule_type causes fail-closed construction."""
        signer = _make_signer()
        bad_data: dict[str, Any] = {
            "metadata": {"policy_id": "p1"},
            "rules": [{"rule_id": "r1"}],  # rule_type missing -> KeyError
        }

        _, result = load_and_verify(bad_data, signer)

        assert result.valid is False

    def test_non_dict_metadata_fails_closed(self) -> None:
        """Non-dict metadata causes fail-closed construction."""
        signer = _make_signer()
        bad_data: dict[str, Any] = {
            "metadata": "not-a-dict",
            "rules": [],
        }

        _, result = load_and_verify(bad_data, signer)

        assert result.valid is False

    def test_empty_rules_valid_when_signed(self, tmp_path: Path) -> None:
        """A bundle with empty rules is valid when signed."""
        signer = _make_signer()
        data = self._bundle_dict(signer, rules=[])

        bundle, result = load_and_verify(data, signer)

        assert result.valid is True
        assert bundle.rules == ()


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialStartupGuard:
    def test_signer_none_returns_valid_false(self, tmp_path: Path) -> None:
        """signer=None always fails (fail-closed)."""
        signer = _make_signer()
        bundle = _signed_bundle(signer)
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(bundle, signer=None, audit_log=audit_log)

        assert result.valid is False

    def test_signer_none_emits_halt_audit_event(self, tmp_path: Path) -> None:
        """signer=None still emits a GOVERNANCE_HALT event when audit_log provided."""
        signer = _make_signer()
        bundle = _signed_bundle(signer)
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer=None, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        assert len(events) == 1
        assert events[0]["event_type"] == "GOVERNANCE_HALT"

    def test_wrong_signer_key_returns_valid_false(self, tmp_path: Path) -> None:
        """A bundle signed with key A is rejected by signer using key B."""
        signer_a = _make_signer(hashlib.sha256(b"key-a").digest())
        signer_b = _make_signer(hashlib.sha256(b"key-b").digest())
        bundle = _signed_bundle(signer_a)
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(bundle, signer_b, audit_log=audit_log)

        assert result.valid is False

    def test_corrupted_signature_fails_verification(self, tmp_path: Path) -> None:
        """A bundle with a corrupted signature string fails verification."""
        signer = _make_signer()
        bundle = _signed_bundle(signer, rules=(_RULE,))
        # Tamper with the signature.
        corrupted = PolicyBundle(
            metadata=bundle.metadata,
            rules=bundle.rules,
            signature="deadbeef" * 16,
        )
        audit_log = _make_audit_log(tmp_path)

        result = verify_policy_or_halt(corrupted, signer, audit_log=audit_log)

        assert result.valid is False

    def test_load_and_verify_with_none_rules_value_fails_closed(self) -> None:
        """None as rules value in bundle_data causes fail-closed construction."""
        signer = _make_signer()
        bad_data: dict[str, Any] = {
            "metadata": {"policy_id": "p1"},
            "rules": None,  # iteration over None -> TypeError
        }

        _, result = load_and_verify(bad_data, signer)

        assert result.valid is False

    def test_audit_event_contains_issuer_startup_guard(self, tmp_path: Path) -> None:
        """Governance audit event has issuer='StartupGuard'."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        data = events[0]["data"]
        assert data["issuer"] == "StartupGuard"

    def test_audit_event_has_non_empty_audit_id(self, tmp_path: Path) -> None:
        """Governance audit event has a non-empty audit_id field."""
        signer = _make_signer()
        bundle = _make_bundle(rules=(_RULE,), signature="")
        audit_log = _make_audit_log(tmp_path)

        verify_policy_or_halt(bundle, signer, audit_log=audit_log)

        events = _read_audit_events(audit_log)
        data = events[0]["data"]
        assert data["audit_id"]
