"""Tests for RollbackGuard: policy rollback protection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from veronica_core.audit.log import AuditLog
from veronica_core.security.rollback_guard import ENGINE_VERSION, RollbackGuard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit_log(tmp_path: Path, filename: str = "rollback_audit.jsonl") -> AuditLog:
    return AuditLog(path=tmp_path / filename)


# ---------------------------------------------------------------------------
# RollbackGuard tests
# ---------------------------------------------------------------------------


class TestRollbackGuardBasic:
    """Core rollback detection and acceptance tests."""

    def test_first_run_empty_log_passes(self, tmp_path: Path) -> None:
        """First run with empty log must pass without error."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        # Should not raise
        guard.check(1)

    def test_normal_upgrade_passes(self, tmp_path: Path) -> None:
        """Upgrading from version 1 to 2 must pass."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        guard.check(1)  # Accept version 1
        guard2 = RollbackGuard(audit_log=log)
        guard2.check(2)  # Upgrade to version 2 — should pass

    def test_rollback_raises_runtime_error(self, tmp_path: Path) -> None:
        """Rollback from version 2 to version 1 must raise RuntimeError."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        guard.check(2)  # Accept version 2

        guard2 = RollbackGuard(audit_log=log)
        with pytest.raises(RuntimeError, match="rollback"):
            guard2.check(1)

    def test_same_version_passes(self, tmp_path: Path) -> None:
        """Re-loading the same policy version must not raise."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        guard.check(1)
        guard2 = RollbackGuard(audit_log=log)
        guard2.check(1)  # Same version — should pass

    def test_rollback_logs_policy_rollback_event(self, tmp_path: Path) -> None:
        """A rollback attempt must write a policy_rollback event to the audit log."""
        log_path = tmp_path / "rollback_audit.jsonl"
        log = AuditLog(path=log_path)
        guard = RollbackGuard(audit_log=log)
        guard.check(5)  # Accept version 5

        guard2 = RollbackGuard(audit_log=log)
        with pytest.raises(RuntimeError):
            guard2.check(3)

        # Scan log for rollback event
        lines = log_path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line)["data"] for line in lines if line.strip()]
        rollback_events = [e for e in events if e.get("event") == "policy_rollback"]
        assert rollback_events, "policy_rollback event not found in audit log"
        assert rollback_events[0]["current_version"] == 3
        assert rollback_events[0]["last_seen_version"] == 5

    def test_checkpoint_written_after_accept(self, tmp_path: Path) -> None:
        """After accepting a policy version, a checkpoint must be written."""
        log_path = tmp_path / "checkpoint_audit.jsonl"
        log = AuditLog(path=log_path)
        guard = RollbackGuard(audit_log=log)
        guard.check(7)

        # Scan log for checkpoint event
        lines = log_path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line)["data"] for line in lines if line.strip()]
        checkpoint_events = [e for e in events if e.get("event") == "policy_checkpoint"]
        assert checkpoint_events, "policy_checkpoint event not found in audit log"
        assert checkpoint_events[-1]["max_policy_version"] == 7

    def test_backward_scan_finds_checkpoint(self, tmp_path: Path) -> None:
        """Backward scan must return the checkpoint value without scanning all entries."""
        log_path = tmp_path / "bscan_audit.jsonl"
        log = AuditLog(path=log_path)

        # Write some unrelated events, then accept version 4
        log.write("SHELL_EXECUTE", {"cmd": "pytest"})
        log.write("ALLOW", {"action": "file_read"})
        guard = RollbackGuard(audit_log=log)
        guard.check(4)

        # get_last_policy_version should return 4 from checkpoint
        result = log.get_last_policy_version()
        assert result == 4


class TestRollbackGuardEngineVersion:
    """Engine version constraint checks."""

    def test_min_engine_version_met_passes(self, tmp_path: Path) -> None:
        """Policy with min_engine_version == ENGINE_VERSION must pass."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        # ENGINE_VERSION is "0.1.0"; same version should pass
        guard.check(1, min_engine_version=ENGINE_VERSION)

    def test_min_engine_version_not_met_raises(self, tmp_path: Path) -> None:
        """Policy requiring a higher engine version must raise RuntimeError."""
        log = _audit_log(tmp_path)
        guard = RollbackGuard(audit_log=log)
        with pytest.raises(RuntimeError, match="Engine"):
            guard.check(1, min_engine_version="99.99.99")

    def test_no_audit_log_engine_version_check_still_works(self) -> None:
        """Engine version check must work even without an audit_log."""
        guard = RollbackGuard(audit_log=None)
        # No audit log — rollback skipped, but engine check still applies
        with pytest.raises(RuntimeError, match="Engine"):
            guard.check(1, min_engine_version="99.0.0")


# ---------------------------------------------------------------------------
# AuditLog policy-specific method tests
# ---------------------------------------------------------------------------


class TestAuditLogPolicyMethods:
    """Tests for the policy version tracking methods added to AuditLog."""

    def test_get_last_policy_version_empty_log_returns_none(self, tmp_path: Path) -> None:
        """get_last_policy_version on empty log must return None."""
        log = _audit_log(tmp_path)
        assert log.get_last_policy_version() is None

    def test_get_last_policy_version_after_checkpoint(self, tmp_path: Path) -> None:
        """After write_policy_checkpoint(5), get_last_policy_version() must return 5."""
        log = _audit_log(tmp_path)
        log.write_policy_checkpoint(5)
        assert log.get_last_policy_version() == 5

    def test_get_last_policy_version_max_of_accepted(self, tmp_path: Path) -> None:
        """After log_policy_version_accepted(3) + (7), must return 7 (max)."""
        log = _audit_log(tmp_path)
        log.log_policy_version_accepted(3, "policies/default.yaml")
        log.log_policy_version_accepted(7, "policies/default.yaml")
        assert log.get_last_policy_version() == 7

    def test_checkpoint_takes_priority_over_accepted_scan(self, tmp_path: Path) -> None:
        """Checkpoint entry must be returned immediately, stopping backward scan."""
        log = _audit_log(tmp_path)
        # Write accepted version 10 first
        log.log_policy_version_accepted(10, "policies/default.yaml")
        # Then a checkpoint for version 8
        log.write_policy_checkpoint(8)
        # Backward scan hits checkpoint first → must return 8, not 10
        assert log.get_last_policy_version() == 8
