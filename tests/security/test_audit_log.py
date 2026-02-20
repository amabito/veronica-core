"""Tests for AuditLog: tamper-evident chained JSONL log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.security.masking import SecretMasker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(tmp_path: Path, filename: str = "audit.jsonl") -> AuditLog:
    """Create a fresh AuditLog backed by a temp file."""
    return AuditLog(path=tmp_path / filename)


def _log_with_masker(tmp_path: Path, filename: str = "masked_audit.jsonl") -> AuditLog:
    """Create an AuditLog with secret masking enabled."""
    return AuditLog(path=tmp_path / filename, masker=SecretMasker())


# ---------------------------------------------------------------------------
# Chain integrity tests
# ---------------------------------------------------------------------------


class TestAuditLogChainIntegrity:
    """Hash chain must be valid after sequential writes."""

    def test_empty_log_verify_chain_returns_true(self, tmp_path: Path) -> None:
        log = _log(tmp_path, "empty.jsonl")
        assert log.verify_chain() is True

    def test_single_entry_verify_chain_returns_true(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write("TEST_EVENT", {"key": "value"})
        assert log.verify_chain() is True

    def test_five_entries_verify_chain_returns_true(self, tmp_path: Path) -> None:
        """Writing 5 entries produces a valid hash chain."""
        log = _log(tmp_path)
        for i in range(5):
            log.write("SHELL_EXECUTE", {"cmd": f"pytest test_{i}.py", "index": i})
        assert log.verify_chain() is True

    def test_verify_chain_after_reopen(self, tmp_path: Path) -> None:
        """A reopened AuditLog must also pass chain verification."""
        log_path = tmp_path / "audit.jsonl"
        log1 = AuditLog(path=log_path)
        for i in range(3):
            log1.write("DENY", {"rule_id": "SHELL_DENY_CMD", "i": i})

        # Open a second instance pointing to the same file
        log2 = AuditLog(path=log_path)
        assert log2.verify_chain() is True

    def test_appended_entries_chain_is_valid(self, tmp_path: Path) -> None:
        """Entries written across two instances share one valid chain."""
        log_path = tmp_path / "audit.jsonl"
        log1 = AuditLog(path=log_path)
        log1.write("ALLOW", {"action": "shell"})
        log1.write("DENY", {"action": "shell", "rule": "SHELL_DENY_CMD"})

        log2 = AuditLog(path=log_path)
        log2.write("ALLOW", {"action": "file_read"})
        assert log2.verify_chain() is True


# ---------------------------------------------------------------------------
# Tamper detection tests
# ---------------------------------------------------------------------------


class TestAuditLogTamperDetection:
    """Any modification to log lines must cause verify_chain() to return False."""

    def test_corrupt_single_line_returns_false(self, tmp_path: Path) -> None:
        """Manually corrupting one line breaks the chain."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        for i in range(3):
            log.write("EVENT", {"i": i})

        # Read lines
        lines = log_path.read_text(encoding="utf-8").splitlines()
        # Corrupt the second line by changing a field value
        entry = json.loads(lines[1])
        entry["event_type"] = "TAMPERED"
        # Recompute hash so the line is valid JSON but chain is broken
        lines[1] = json.dumps(entry, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered_log = AuditLog(path=log_path)
        assert tampered_log.verify_chain() is False

    def test_corrupt_hash_field_returns_false(self, tmp_path: Path) -> None:
        """Changing the stored hash field directly breaks the chain."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        log.write("EVENT_A", {"x": 1})
        log.write("EVENT_B", {"x": 2})

        lines = log_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["hash"] = "a" * 64  # wrong hash
        lines[0] = json.dumps(entry, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered_log = AuditLog(path=log_path)
        assert tampered_log.verify_chain() is False

    def test_deleted_line_returns_false(self, tmp_path: Path) -> None:
        """Deleting an entry breaks the prev_hash linkage."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        log.write("FIRST", {})
        log.write("SECOND", {})
        log.write("THIRD", {})

        # Remove the second line
        lines = log_path.read_text(encoding="utf-8").splitlines()
        lines.pop(1)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered_log = AuditLog(path=log_path)
        assert tampered_log.verify_chain() is False

    def test_invalid_json_line_returns_false(self, tmp_path: Path) -> None:
        """A non-JSON line in the log causes verify_chain() to return False."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        log.write("EVENT", {"ok": True})

        # Append a garbage line
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("NOT VALID JSON\n")

        tampered_log = AuditLog(path=log_path)
        assert tampered_log.verify_chain() is False


# ---------------------------------------------------------------------------
# Secret masking tests
# ---------------------------------------------------------------------------


class TestAuditLogSecretMasking:
    """Sensitive keys in data must be redacted in the log file."""

    def test_aws_key_is_masked_in_log(self, tmp_path: Path) -> None:
        """AWS access key in log data must be redacted.

        AWS access key IDs are exactly 20 characters: 'AKIA' + 16 uppercase
        alphanumeric characters, terminated by a word boundary.
        """
        # Valid AWS access key: AKIA + exactly 16 uppercase alphanumeric chars
        aws_key = "AKIAIOSFODNN7EXAMPLE"  # 20 chars, word-boundary friendly
        log = _log_with_masker(tmp_path)
        log.write("SHELL_EXECUTE", {"cmd": "aws s3 ls", "key": aws_key})

        content = (tmp_path / "masked_audit.jsonl").read_text(encoding="utf-8")
        assert aws_key not in content
        assert "REDACTED" in content

    def test_password_kv_is_masked_in_log(self, tmp_path: Path) -> None:
        """password=<value> in log data must be redacted."""
        log = _log_with_masker(tmp_path)
        log.write("AUTH", {"header": "Authorization: password=supersecret123"})

        content = (tmp_path / "masked_audit.jsonl").read_text(encoding="utf-8")
        assert "supersecret123" not in content

    def test_chain_valid_after_masked_write(self, tmp_path: Path) -> None:
        """Masking does not break the hash chain."""
        log = _log_with_masker(tmp_path)
        log.write("DENY", {"reason": "curl blocked", "api_key": "myapikey=abc123"})
        log.write("ALLOW", {"cmd": "pytest"})
        assert log.verify_chain() is True


# ---------------------------------------------------------------------------
# Entry structure tests
# ---------------------------------------------------------------------------


class TestAuditLogEntryStructure:
    """Each log entry must contain required fields."""

    def test_entry_has_required_fields(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write("TEST", {"foo": "bar"})

        line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        for field in ("ts", "event_type", "data", "prev_hash", "hash"):
            assert field in entry, f"Missing field: {field}"

    def test_first_entry_prev_hash_is_genesis(self, tmp_path: Path) -> None:
        """First entry's prev_hash must be the genesis hash (64 zeros)."""
        log = _log(tmp_path)
        log.write("FIRST", {})

        line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["prev_hash"] == "0" * 64

    def test_second_entry_prev_hash_matches_first_hash(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.write("FIRST", {"x": 1})
        log.write("SECOND", {"x": 2})

        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert second["prev_hash"] == first["hash"]
