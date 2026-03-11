"""Tests for signed AuditLog entries: HMAC signing, verify_chain with signer,
write_governance_event, and PolicySigner.sign_bytes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.security.policy_signing import PolicySigner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_KEY = hashlib.sha256(b"audit-test-key").digest()


def _make_signer(key: bytes = _TEST_KEY) -> PolicySigner:
    return PolicySigner(key=key)


def _read_entries(path: Path) -> list[dict[str, Any]]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# sign_bytes
# ---------------------------------------------------------------------------


def test_sign_bytes_consistent_for_same_input() -> None:
    signer = _make_signer()
    data = b"hello world"
    assert signer.sign_bytes(data) == signer.sign_bytes(data)


def test_sign_bytes_different_for_different_input() -> None:
    signer = _make_signer()
    assert signer.sign_bytes(b"aaa") != signer.sign_bytes(b"bbb")


def test_sign_bytes_returns_hex_string() -> None:
    signer = _make_signer()
    result = signer.sign_bytes(b"test")
    # Must be valid hex of the right length (SHA256 = 64 hex chars).
    assert len(result) == 64
    int(result, 16)  # raises ValueError if not valid hex


def test_sign_bytes_empty_input() -> None:
    signer = _make_signer()
    result = signer.sign_bytes(b"")
    assert isinstance(result, str)
    assert len(result) == 64


# ---------------------------------------------------------------------------
# AuditLog with signer: entries have "hmac" field
# ---------------------------------------------------------------------------


def test_audit_log_with_signer_entries_have_hmac(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("TEST_EVENT", {"key": "value"})
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert len(entries) == 1
    assert "hmac" in entries[0]
    assert isinstance(entries[0]["hmac"], str)
    assert len(entries[0]["hmac"]) == 64


def test_audit_log_without_signer_entries_have_no_hmac(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write("TEST_EVENT", {"key": "value"})
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert len(entries) == 1
    assert "hmac" not in entries[0]


def test_audit_log_multiple_entries_all_have_hmac(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    for i in range(5):
        log.write("EVENT", {"i": i})
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert all("hmac" in e for e in entries)


# ---------------------------------------------------------------------------
# verify_chain with signer
# ---------------------------------------------------------------------------


def test_verify_chain_with_signer_passes(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EV1", {"x": 1})
    log.write("EV2", {"x": 2})
    assert log.verify_chain(signer=signer) is True


def test_verify_chain_without_signer_still_passes_hash_chain(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EV1", {"x": 1})
    # No signer passed to verify_chain -- only hash chain is checked.
    assert log.verify_chain() is True


def test_verify_chain_detects_tampered_hmac(tmp_path: Path) -> None:
    signer = _make_signer()
    log_path = tmp_path / "audit.jsonl"
    log = AuditLog(log_path, signer=signer)
    log.write("EV1", {"x": 1})

    # Tamper the HMAC field directly in the file.
    entries = _read_entries(log_path)
    entries[0]["hmac"] = "0" * 64
    log_path.write_text(
        "\n".join(json.dumps(e, separators=(",", ":")) for e in entries) + "\n",
        encoding="utf-8",
    )

    assert log.verify_chain(signer=signer) is False


def test_verify_chain_wrong_key_detects_mismatch(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EV1", {"x": 1})
    # Verify with a different key -- HMAC mismatch must be detected.
    wrong_signer = _make_signer(key=hashlib.sha256(b"wrong-key").digest())
    assert log.verify_chain(signer=wrong_signer) is False


def test_verify_chain_empty_log_is_valid(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    log = AuditLog(log_path)
    assert log.verify_chain() is True


# ---------------------------------------------------------------------------
# write_governance_event
# ---------------------------------------------------------------------------


def test_write_governance_event_writes_all_fields(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    audit_id = str(uuid.uuid4())
    log.write_governance_event(
        event_type="GOVERNANCE_HALT",
        decision="HALT",
        reason_code="POLICY_EXPIRED",
        reason="Policy epoch has expired",
        audit_id=audit_id,
        policy_hash="abc123",
        policy_epoch=42,
        issuer="veronica-core",
    )
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert len(entries) == 1
    data = entries[0]["data"]
    assert data["decision"] == "HALT"
    assert data["reason_code"] == "POLICY_EXPIRED"
    assert data["reason"] == "Policy epoch has expired"
    assert data["audit_id"] == audit_id
    assert data["policy_hash"] == "abc123"
    assert data["policy_epoch"] == 42
    assert data["issuer"] == "veronica-core"
    assert entries[0]["event_type"] == "GOVERNANCE_HALT"


def test_write_governance_event_with_metadata(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write_governance_event(
        event_type="GOVERNANCE_DEGRADE",
        decision="DEGRADE",
        reason_code="PARTIAL_FAILURE",
        reason="Component degraded",
        audit_id="a1",
        policy_hash="h1",
        policy_epoch=1,
        issuer="sys",
        metadata={"extra": "info", "count": 3},
    )
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert entries[0]["data"]["metadata"] == {"extra": "info", "count": 3}


def test_write_governance_event_no_metadata_key_absent(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write_governance_event(
        event_type="GOVERNANCE_QUARANTINE",
        decision="QUARANTINE",
        reason_code="TAINT",
        reason="Tainted agent",
        audit_id="q1",
        policy_hash="h2",
        policy_epoch=0,
        issuer="sys",
    )
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert "metadata" not in entries[0]["data"]


def test_write_governance_event_is_signed_when_signer_present(tmp_path: Path) -> None:
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write_governance_event(
        event_type="GOVERNANCE_HALT",
        decision="HALT",
        reason_code="EXPIRED",
        reason="expired",
        audit_id="gid1",
        policy_hash="ph1",
        policy_epoch=1,
        issuer="sys",
    )
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert "hmac" in entries[0]
    # Chain verification with signer must also pass.
    assert log.verify_chain(signer=signer) is True


# ---------------------------------------------------------------------------
# Adversarial cases
# ---------------------------------------------------------------------------


def test_signer_that_raises_on_sign_bytes_does_not_crash_write(tmp_path: Path) -> None:
    """If signer.sign_bytes raises, AuditLog.write must propagate the exception.

    The HMAC is security-critical -- silently swallowing the error would
    produce an unsigned entry that appears signed.  The write method does not
    catch signer errors; callers are responsible for providing a healthy signer.
    """

    class FailSigner:
        def sign_bytes(self, data: bytes) -> str:
            raise RuntimeError("signer offline")

    log = AuditLog(tmp_path / "audit.jsonl", signer=FailSigner())
    with pytest.raises(RuntimeError, match="signer offline"):
        log.write("EV", {"k": "v"})


def test_verify_chain_signer_that_raises_returns_false(tmp_path: Path) -> None:
    """If the signer passed to verify_chain raises, the result is False."""
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EV1", {"x": 1})

    class FailSigner:
        def sign_bytes(self, data: bytes) -> str:
            raise RuntimeError("boom")

    assert log.verify_chain(signer=FailSigner()) is False


def test_audit_log_with_signer_empty_event_data(tmp_path: Path) -> None:
    """Empty data dict must still produce a signed entry."""
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EMPTY", {})
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert "hmac" in entries[0]
    assert log.verify_chain(signer=signer) is True


def test_hmac_covers_entry_content(tmp_path: Path) -> None:
    """Two entries with different data must have different HMACs."""
    signer = _make_signer()
    log = AuditLog(tmp_path / "audit.jsonl", signer=signer)
    log.write("EV", {"x": 1})
    log.write("EV", {"x": 2})
    entries = _read_entries(tmp_path / "audit.jsonl")
    assert entries[0]["hmac"] != entries[1]["hmac"]
