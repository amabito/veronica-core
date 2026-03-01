"""Tests for CLIApprover: sign/verify/approve round-trip and edge cases."""
from __future__ import annotations

import warnings
from datetime import datetime, timezone, timedelta


from veronica_core.approval.approver import (
    ApprovalRequest,
    ApprovalToken,
    CLIApprover,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STABLE_KEY = b"veronica-test-secret-key-32bytes"


def _approver(key: bytes | None = _STABLE_KEY) -> CLIApprover:
    return CLIApprover(secret_key=key)


def _request(
    approver: CLIApprover,
    rule_id: str = "FILE_WRITE_REQUIRE_APPROVAL",
    action: str = "file_write",
    args: list[str] | None = None,
) -> ApprovalRequest:
    return approver.create_request(rule_id, action, args or [".github/workflows/ci.yml"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSignAndVerifyRoundTrip:
    """Sign + verify round-trip must pass."""

    def test_sign_then_verify_returns_true(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        assert approver.verify(token) is True

    def test_approve_fresh_token_returns_true(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        assert approver.approve(token) is True

    def test_token_fields_match_request(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        assert token.rule_id == request.rule_id
        assert token.action == request.action
        assert token.args_hash == request.args_hash
        assert token.timestamp == request.timestamp

    def test_request_id_is_unique(self) -> None:
        approver = _approver()
        req = _request(approver)
        t1 = approver.sign(req)
        t2 = approver.sign(req)
        assert t1.request_id != t2.request_id


class TestTamperedToken:
    """Tampered tokens must fail verification."""

    def test_tampered_signature_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        bad_token = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature="deadbeef" * 8,  # wrong signature
        )
        assert approver.verify(bad_token) is False

    def test_tampered_rule_id_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        bad_token = ApprovalToken(
            request_id=token.request_id,
            rule_id="DIFFERENT_RULE",
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature=token.signature,
        )
        assert approver.verify(bad_token) is False

    def test_tampered_args_hash_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign(request)
        bad_token = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash="a" * 64,  # wrong hash
            timestamp=token.timestamp,
            signature=token.signature,
        )
        assert approver.verify(bad_token) is False

    def test_wrong_key_verify_returns_false(self) -> None:
        signer = CLIApprover(secret_key=b"key-one-" + b"x" * 24)
        verifier = CLIApprover(secret_key=b"key-two-" + b"y" * 24)
        request = signer.create_request("RULE", "file_write", ["f.sh"])
        token = signer.sign(request)
        assert verifier.verify(token) is False


class TestExpiredToken:
    """Tokens older than 5 minutes must be rejected by approve()."""

    def test_expired_token_approve_returns_false(self) -> None:
        approver = _approver()
        # Manually build a token with an old timestamp
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        request = ApprovalRequest(
            rule_id="RULE",
            action="file_write",
            args_hash="a" * 64,
            timestamp=old_ts,
        )
        token = approver.sign(request)
        # verify() should still pass (signature is valid)
        assert approver.verify(token) is True
        # approve() must fail because the token is > 5 min old
        assert approver.approve(token) is False

    def test_invalid_timestamp_approve_returns_false(self) -> None:
        approver = _approver()
        request = ApprovalRequest(
            rule_id="RULE",
            action="shell",
            args_hash="b" * 64,
            timestamp="not-a-date",
        )
        token = approver.sign(request)
        assert approver.approve(token) is False


class TestEphemeralKey:
    """Ephemeral key mode emits a warning but still signs correctly."""

    def test_ephemeral_key_emits_warning(self) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            CLIApprover(secret_key=None)
        assert any("ephemeral" in str(warning.message).lower() for warning in w)

    def test_ephemeral_key_sign_verify_works(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            approver = CLIApprover(secret_key=None)
        request = approver.create_request("RULE", "shell", ["pytest"])
        token = approver.sign(request)
        assert approver.verify(token) is True
