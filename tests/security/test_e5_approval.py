"""E-5: Approval token hardening tests.

Covers:
- sign_v2 + approve round-trip → True
- replay: same token used twice → second approve returns False
- tampered nonce → verify False
- expired token (mock time) → approve False
- wrong scope (different args_hash) → approve False
- audit_log receives APPROVAL_GRANTED event on success
- audit_log receives APPROVAL_DENIED event on replay
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from veronica_core.approval.approver import (
    ApprovalRequest,
    ApprovalToken,
    CLIApprover,
    NonceRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STABLE_KEY = b"veronica-e5-test-secret-key-32by"


def _approver() -> CLIApprover:
    return CLIApprover(secret_key=_STABLE_KEY)


def _request(
    approver: CLIApprover,
    rule_id: str = "FILE_WRITE_REQUIRE_APPROVAL",
    action: str = "file_write",
    args: list[str] | None = None,
) -> ApprovalRequest:
    return approver.create_request(rule_id, action, args or [".github/workflows/ci.yml"])


# ---------------------------------------------------------------------------
# sign_v2 basic round-trip
# ---------------------------------------------------------------------------

class TestSignV2RoundTrip:
    def test_sign_v2_then_approve_returns_true(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        assert approver.approve(token) is True

    def test_sign_v2_token_has_v2_fields(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        assert token.nonce != ""
        assert token.scope != ""
        assert token.expiry != ""

    def test_sign_v2_scope_format(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        expected_scope = f"{request.action}:{request.args_hash}"
        assert token.scope == expected_scope

    def test_sign_v2_verify_returns_true(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        assert approver.verify(token) is True

    def test_sign_v2_request_id_is_unique(self) -> None:
        approver = _approver()
        request = _request(approver)
        token1 = approver.sign_v2(request)
        token2 = approver.sign_v2(request)
        assert token1.request_id != token2.request_id

    def test_sign_v2_nonce_is_unique(self) -> None:
        approver = _approver()
        request = _request(approver)
        token1 = approver.sign_v2(request)
        token2 = approver.sign_v2(request)
        assert token1.nonce != token2.nonce


# ---------------------------------------------------------------------------
# Replay prevention
# ---------------------------------------------------------------------------

class TestReplayPrevention:
    def test_approve_same_token_twice_second_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        assert approver.approve(token) is True
        assert approver.approve(token) is False

    def test_different_tokens_both_approved(self) -> None:
        approver = _approver()
        request = _request(approver)
        token1 = approver.sign_v2(request)
        token2 = approver.sign_v2(request)
        assert approver.approve(token1) is True
        assert approver.approve(token2) is True

    def test_replay_across_multiple_attempts_all_fail(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        assert approver.approve(token) is True
        for _ in range(5):
            assert approver.approve(token) is False


# ---------------------------------------------------------------------------
# Tampered token detection
# ---------------------------------------------------------------------------

class TestTamperedToken:
    def test_tampered_nonce_verify_returns_false(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature=token.signature,
            expiry=token.expiry,
            nonce="00000000000000000000000000000000",  # tampered
            scope=token.scope,
        )
        assert approver.verify(tampered) is False

    def test_tampered_scope_verify_returns_false(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature=token.signature,
            expiry=token.expiry,
            nonce=token.nonce,
            scope="file_write:tampered_hash",  # tampered
        )
        assert approver.verify(tampered) is False

    def test_tampered_signature_verify_returns_false(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature="deadbeef" * 8,  # tampered
            expiry=token.expiry,
            nonce=token.nonce,
            scope=token.scope,
        )
        assert approver.verify(tampered) is False


# ---------------------------------------------------------------------------
# Expired token
# ---------------------------------------------------------------------------

class TestExpiredToken:
    def test_expired_token_approve_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        # Patch datetime.now to simulate time after expiry
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch("veronica_core.approval.approver.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            assert approver.approve(token) is False

    def test_fresh_token_is_not_expired(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        assert approver.approve(token) is True


# ---------------------------------------------------------------------------
# Scope mismatch
# ---------------------------------------------------------------------------

class TestScopeMismatch:
    def test_wrong_scope_approve_returns_false(self) -> None:
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        # Construct a token with a different args_hash but same nonce/signature
        # (signature will fail because of tampered args_hash)
        different_hash = hashlib.sha256(b"different_args").hexdigest()
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=different_hash,  # changed
            timestamp=token.timestamp,
            signature=token.signature,  # original sig won't match
            expiry=token.expiry,
            nonce=token.nonce,
            scope=token.scope,  # original scope
        )
        assert approver.approve(tampered) is False

    def test_tampered_scope_field_approve_returns_false(self) -> None:
        """Token with modified scope field but original nonce should fail verify."""
        approver = _approver()
        request = _request(approver)
        token = approver.sign_v2(request)
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature=token.signature,
            expiry=token.expiry,
            nonce=token.nonce,
            scope=f"net:{token.args_hash}",  # wrong action in scope
        )
        assert approver.approve(tampered) is False


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------

class TestAuditLogIntegration:
    def test_audit_log_receives_approval_granted_on_success(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog
        log_path = tmp_path / "audit.jsonl"
        audit_log = AuditLog(log_path)
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        result = approver.approve(token, audit_log=audit_log)
        assert result is True
        # Verify log was written
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        import json
        entry = json.loads(lines[-1])
        assert entry["event_type"] == "APPROVAL_GRANTED"
        assert entry["data"]["action"] == "file_write"

    def test_audit_log_receives_approval_denied_on_replay(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog
        log_path = tmp_path / "audit.jsonl"
        audit_log = AuditLog(log_path)
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        # First approval: granted
        assert approver.approve(token, audit_log=audit_log) is True
        # Second approval: denied (replay)
        assert approver.approve(token, audit_log=audit_log) is False
        import json
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        event_types = [e["event_type"] for e in entries]
        assert "APPROVAL_GRANTED" in event_types
        assert "APPROVAL_DENIED" in event_types

    def test_audit_log_denied_on_invalid_signature(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog
        log_path = tmp_path / "audit.jsonl"
        audit_log = AuditLog(log_path)
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        tampered = ApprovalToken(
            request_id=token.request_id,
            rule_id=token.rule_id,
            action=token.action,
            args_hash=token.args_hash,
            timestamp=token.timestamp,
            signature="bad" * 20,
            expiry=token.expiry,
            nonce=token.nonce,
            scope=token.scope,
        )
        result = approver.approve(tampered, audit_log=audit_log)
        assert result is False
        import json
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event_type"] == "APPROVAL_DENIED"

    def test_audit_log_none_does_not_raise(self) -> None:
        approver = _approver()
        token = approver.sign_v2(_request(approver))
        result = approver.approve(token, audit_log=None)
        assert result is True


# ---------------------------------------------------------------------------
# NonceRegistry
# ---------------------------------------------------------------------------

class TestNonceRegistry:
    def test_fresh_nonce_returns_true(self) -> None:
        registry = NonceRegistry()
        assert registry.consume("abc123") is True

    def test_duplicate_nonce_returns_false(self) -> None:
        registry = NonceRegistry()
        registry.consume("abc123")
        assert registry.consume("abc123") is False

    def test_different_nonces_both_fresh(self) -> None:
        registry = NonceRegistry()
        assert registry.consume("nonce1") is True
        assert registry.consume("nonce2") is True

    def test_clear_expired_allows_reuse(self) -> None:
        registry = NonceRegistry()
        registry.consume("abc123")
        registry.clear_expired()
        # After clearing, nonce can be reused
        assert registry.consume("abc123") is True

    def test_max_size_eviction(self) -> None:
        # max_size=2: n1, n2 fill the registry
        registry = NonceRegistry(max_size=2)
        registry.consume("n1")
        registry.consume("n2")
        # n3 triggers eviction of n1 (oldest); registry now holds [n2, n3]
        registry.consume("n3")
        # n1 was evicted — can be consumed again
        assert registry.consume("n1") is True
        # n3 is still tracked — replay should be detected
        assert registry.consume("n3") is False

    def test_thread_safety(self) -> None:
        registry = NonceRegistry()
        results: list[bool] = []
        lock = threading.Lock()

        def consume(nonce: str) -> None:
            result = registry.consume(nonce)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=consume, args=("shared_nonce",)) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one consume should return True
        assert results.count(True) == 1
        assert results.count(False) == 49


# ---------------------------------------------------------------------------
# Backward compatibility — v1 sign still works
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_v1_sign_approve_still_works(self) -> None:
        approver = _approver()
        request = _request(approver)
        with pytest.warns(DeprecationWarning):
            token = approver.sign(request)
        assert approver.approve(token) is True

    def test_v1_token_has_no_v2_fields(self) -> None:
        approver = _approver()
        request = _request(approver)
        with pytest.warns(DeprecationWarning):
            token = approver.sign(request)
        assert token.nonce == ""
        assert token.scope == ""
        assert token.expiry == ""
