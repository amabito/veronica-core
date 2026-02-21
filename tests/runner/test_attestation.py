"""Tests for AttestationChecker and EnvironmentFingerprint (G-3)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from veronica_core.runner.attestation import (
    AttestationChecker,
    EnvironmentFingerprint,
)


# ---------------------------------------------------------------------------
# EnvironmentFingerprint tests
# ---------------------------------------------------------------------------


class TestEnvironmentFingerprint:
    def test_capture_returns_fingerprint(self) -> None:
        fp = EnvironmentFingerprint.capture()
        assert isinstance(fp, EnvironmentFingerprint)

    def test_capture_platform_matches_sys(self) -> None:
        fp = EnvironmentFingerprint.capture()
        assert fp.platform == sys.platform

    def test_capture_python_path_matches_sys(self) -> None:
        fp = EnvironmentFingerprint.capture()
        assert fp.python_path == sys.executable

    def test_capture_cwd_matches_os(self) -> None:
        fp = EnvironmentFingerprint.capture()
        assert fp.cwd == os.getcwd()

    def test_uid_is_none_when_getuid_unavailable(self) -> None:
        # Simulate a platform without os.getuid (Windows)
        # We temporarily hide hasattr(os, "getuid") by patching the attribute
        _orig = getattr(os, "getuid", None)
        try:
            if hasattr(os, "getuid"):
                delattr(os, "getuid")  # type: ignore[misc]
            fp = EnvironmentFingerprint.capture()
            assert fp.uid is None
        finally:
            if _orig is not None:
                os.getuid = _orig  # type: ignore[attr-defined]

    def test_fingerprint_is_frozen(self) -> None:
        fp = EnvironmentFingerprint.capture()
        with pytest.raises((AttributeError, TypeError)):
            fp.platform = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AttestationChecker tests
# ---------------------------------------------------------------------------


class TestAttestationCheckerBaseline:
    def test_check_returns_true_for_unchanged_env(self) -> None:
        checker = AttestationChecker()
        assert checker.check() is True

    def test_baseline_is_accessible(self) -> None:
        checker = AttestationChecker()
        assert isinstance(checker.baseline, EnvironmentFingerprint)


class TestAttestationCheckerAnomalyDetection:
    def _checker_with_forced_baseline(
        self,
        overrides: dict,
        audit_log=None,
    ) -> AttestationChecker:
        """Build a checker whose baseline has specific values."""
        real = EnvironmentFingerprint.capture()
        fake_baseline = EnvironmentFingerprint(
            username=overrides.get("username", real.username),
            platform=overrides.get("platform", real.platform),
            python_path=overrides.get("python_path", real.python_path),
            cwd=overrides.get("cwd", real.cwd),
            uid=overrides.get("uid", real.uid),
        )
        checker = AttestationChecker(audit_log=audit_log)
        object.__setattr__(checker, "_baseline", fake_baseline)
        return checker

    def test_cwd_change_returns_false(self, tmp_path: Path) -> None:
        checker = self._checker_with_forced_baseline({"cwd": "/nonexistent/original/dir"})
        # current cwd differs from baseline
        assert checker.check() is False

    def test_username_change_returns_false(self) -> None:
        checker = self._checker_with_forced_baseline({"username": "__ghost_user__"})
        assert checker.check() is False

    def test_python_path_change_returns_false(self) -> None:
        checker = self._checker_with_forced_baseline({"python_path": "/usr/bin/fake_python"})
        assert checker.check() is False

    def test_anomaly_writes_to_audit_log(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog

        log_path = tmp_path / "audit.jsonl"
        audit_log = AuditLog(log_path)

        checker = self._checker_with_forced_baseline(
            {"username": "__ghost_user__"},
            audit_log=audit_log,
        )
        result = checker.check()
        assert result is False

        # Verify the audit log was written
        assert log_path.exists()
        import json
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert any(e["event_type"] == "ATTESTATION_ANOMALY" for e in entries)

    def test_no_audit_log_anomaly_still_returns_false(self) -> None:
        checker = self._checker_with_forced_baseline({"cwd": "/definitely/not/real"})
        # No audit log — should still return False without raising
        assert checker.check() is False
