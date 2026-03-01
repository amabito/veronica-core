"""Tests for SandboxProbe and ProbeResult (I-2)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from veronica_core.runner.attestation import ProbeResult, SandboxProbe


# ---------------------------------------------------------------------------
# ProbeResult
# ---------------------------------------------------------------------------


class TestProbeResult:
    def test_dataclass_fields(self) -> None:
        result = ProbeResult(
            name="read_probe",
            expected="BLOCKED",
            actual="BLOCKED",
            passed=True,
        )
        assert result.name == "read_probe"
        assert result.expected == "BLOCKED"
        assert result.actual == "BLOCKED"
        assert result.passed is True


# ---------------------------------------------------------------------------
# SandboxProbe.probe_read
# ---------------------------------------------------------------------------


class TestProbeRead:
    def test_permission_error_means_blocked(self) -> None:
        """PermissionError -> sandbox is blocking -> passed=True."""
        probe = SandboxProbe(read_target="/fake/path")
        with patch("veronica_core.runner.attestation.Path") as mock_path_cls:
            mock_path_cls.return_value.stat.side_effect = PermissionError("denied")
            result = probe.probe_read()

        assert result.passed is True
        assert result.actual == "BLOCKED"
        assert result.name == "read_probe"

    def test_oserror_access_denied_means_blocked(self) -> None:
        """OSError with 'access denied' -> sandbox is blocking -> passed=True."""
        probe = SandboxProbe(read_target="/fake/path")
        with patch("veronica_core.runner.attestation.Path") as mock_path_cls:
            mock_path_cls.return_value.stat.side_effect = OSError("Access denied")
            result = probe.probe_read()

        assert result.passed is True
        assert result.actual == "BLOCKED"

    def test_successful_stat_means_allowed(self) -> None:
        """Stat succeeds -> sandbox is NOT blocking -> passed=False."""
        probe = SandboxProbe(read_target="/fake/path")
        with patch("veronica_core.runner.attestation.Path") as mock_path_cls:
            mock_path_cls.return_value.stat.return_value = MagicMock()
            result = probe.probe_read()

        assert result.passed is False
        assert result.actual == "ALLOWED"

    def test_generic_oserror_means_not_passed(self) -> None:
        """OSError without 'access denied' (e.g. file not found) -> passed=False."""
        probe = SandboxProbe(read_target="/fake/path")
        with patch("veronica_core.runner.attestation.Path") as mock_path_cls:
            mock_path_cls.return_value.stat.side_effect = OSError("No such file or directory")
            result = probe.probe_read()

        assert result.passed is False
        assert result.actual.startswith("ERROR:")


# ---------------------------------------------------------------------------
# SandboxProbe.probe_net
# ---------------------------------------------------------------------------


class TestProbeNet:
    def test_connection_refused_means_blocked(self) -> None:
        """ConnectionRefusedError -> sandbox is blocking -> passed=True."""
        probe = SandboxProbe(net_target="http://example.com")
        with patch("veronica_core.runner.attestation.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = ConnectionRefusedError("refused")
            result = probe.probe_net()

        assert result.passed is True
        assert result.actual == "BLOCKED"
        assert result.name == "net_probe"

    def test_oserror_means_blocked(self) -> None:
        """OSError (network unreachable etc.) -> sandbox blocking -> passed=True."""
        probe = SandboxProbe(net_target="http://example.com")
        with patch("veronica_core.runner.attestation.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = OSError("Network unreachable")
            result = probe.probe_net()

        assert result.passed is True
        assert result.actual == "BLOCKED"

    def test_successful_response_means_allowed(self) -> None:
        """HTTP 200 response -> sandbox is NOT blocking -> passed=False."""
        probe = SandboxProbe(net_target="http://example.com")
        with patch("veronica_core.runner.attestation.urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock()
            result = probe.probe_net()

        assert result.passed is False
        assert result.actual == "ALLOWED"

    def test_generic_exception_means_blocked(self) -> None:
        """Timeout or other exception -> treat as blocked -> passed=True."""

        probe = SandboxProbe(net_target="http://example.com")
        with patch("veronica_core.runner.attestation.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = TimeoutError("timed out")
            result = probe.probe_net()

        assert result.passed is True


# ---------------------------------------------------------------------------
# SandboxProbe.run_all
# ---------------------------------------------------------------------------


class TestRunAll:
    def _make_probe_with_mocks(
        self,
        read_passed: bool,
        net_passed: bool,
        audit_log=None,
    ) -> SandboxProbe:
        probe = SandboxProbe(audit_log=audit_log, read_target="/fake", net_target="http://x")
        probe.probe_read = lambda: ProbeResult(  # type: ignore[assignment]
            name="read_probe",
            expected="BLOCKED",
            actual="BLOCKED" if read_passed else "ALLOWED",
            passed=read_passed,
        )
        probe.probe_net = lambda: ProbeResult(  # type: ignore[assignment]
            name="net_probe",
            expected="BLOCKED",
            actual="BLOCKED" if net_passed else "ALLOWED",
            passed=net_passed,
        )
        return probe

    def test_run_all_returns_two_results(self) -> None:
        probe = self._make_probe_with_mocks(read_passed=True, net_passed=True)
        results = probe.run_all()
        assert len(results) == 2

    def test_sandbox_mode_true_failure_returns_failed_results(self) -> None:
        """In sandbox_mode=True, failed probes are returned (caller triggers SAFE_MODE)."""
        probe = self._make_probe_with_mocks(read_passed=False, net_passed=True)
        results = probe.run_all(sandbox_mode=True)
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1
        assert failed[0].name == "read_probe"

    def test_dev_mode_failure_still_returns_results(self) -> None:
        """In dev mode (sandbox_mode=False), failures are returned but not a SAFE_MODE trigger."""
        probe = self._make_probe_with_mocks(read_passed=False, net_passed=False)
        results = probe.run_all(sandbox_mode=False)
        assert len(results) == 2
        assert all(not r.passed for r in results)

    def test_audit_log_written_on_run_all(self, tmp_path: Path) -> None:
        """run_all() writes an audit log event."""
        from veronica_core.audit.log import AuditLog

        log_path = tmp_path / "probe_audit.jsonl"
        audit_log = AuditLog(log_path)

        probe = self._make_probe_with_mocks(read_passed=True, net_passed=True, audit_log=audit_log)
        probe.run_all(sandbox_mode=True)

        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert any(
            e["event_type"] in ("SANDBOX_PROBE_OK", "SANDBOX_PROBE_FAILURE")
            for e in entries
        )

    def test_audit_log_written_on_failure(self, tmp_path: Path) -> None:
        """When a probe fails, audit log event is SANDBOX_PROBE_FAILURE."""
        from veronica_core.audit.log import AuditLog

        log_path = tmp_path / "probe_audit.jsonl"
        audit_log = AuditLog(log_path)

        probe = self._make_probe_with_mocks(read_passed=False, net_passed=True, audit_log=audit_log)
        probe.run_all(sandbox_mode=True)

        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert any(e["event_type"] == "SANDBOX_PROBE_FAILURE" for e in entries)
