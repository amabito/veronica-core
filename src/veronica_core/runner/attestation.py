"""Runner attestation for VERONICA Security Containment Layer.

Captures an environment fingerprint at startup and detects anomalies
(e.g. user switching, working directory changes) at runtime.

Also provides active sandbox probing via :class:`SandboxProbe` which
verifies that the sandbox actually blocks filesystem and network access.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.audit.log import AuditLog


# ---------------------------------------------------------------------------
# EnvironmentFingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentFingerprint:
    """Immutable snapshot of the execution environment.

    Args:
        username: Current OS username (from os.environ or os.getlogin).
        platform: sys.platform value (e.g. "win32", "linux").
        python_path: Path to the Python interpreter (sys.executable).
        cwd: Current working directory at capture time.
        uid: POSIX user ID (None on Windows where os.getuid is unavailable).
    """

    username: str
    platform: str
    python_path: str
    cwd: str
    uid: int | None

    @classmethod
    def capture(cls) -> "EnvironmentFingerprint":
        """Capture the current environment and return a fingerprint.

        Returns:
            EnvironmentFingerprint with current environment values.
        """
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        try:
            # os.getlogin() can fail in containerized / daemonized environments
            if not username:
                username = os.getlogin()
        except OSError:
            pass

        uid: int | None = None
        if hasattr(os, "getuid"):
            uid = os.getuid()

        return cls(
            username=username,
            platform=sys.platform,
            python_path=sys.executable,
            cwd=os.getcwd(),
            uid=uid,
        )


# ---------------------------------------------------------------------------
# SandboxProbe
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result of a single sandbox probe.

    Args:
        name: Short identifier for the probe (e.g. "read_probe").
        expected: What we expect the sandbox to enforce ("BLOCKED").
        actual: What we observed ("BLOCKED", "ALLOWED", or "ERROR:<msg>").
        passed: True when the sandbox correctly blocked the action.
    """

    name: str
    expected: str  # "BLOCKED"
    actual: str    # "BLOCKED" or "ALLOWED" or "ERROR:<msg>"
    passed: bool


class SandboxProbe:
    """Active probes that verify the sandbox is actually enforcing restrictions.

    Two probes are available:

    * :meth:`probe_read` — attempts to stat a protected path; expects
      ``PermissionError`` or ``OSError`` (access denied) to be raised,
      indicating that the sandbox is blocking filesystem access.

    * :meth:`probe_net` — attempts a network request; expects a connection
      error, indicating that the sandbox is blocking outbound network access.

    :meth:`run_all` aggregates all probes.  In sandbox mode, any failure
    (i.e. the sandbox *allows* an action it should block) is a security
    violation.

    Args:
        audit_log: Optional AuditLog to record probe events.
        read_target: Path to probe for filesystem access restrictions.
            Defaults to a platform-appropriate protected system path.
        net_target: URL to probe for network restrictions.
    """

    _DEFAULT_NET_TARGET = "http://example.com"

    def __init__(
        self,
        audit_log: "AuditLog | None" = None,
        read_target: str | None = None,
        net_target: str | None = None,
    ) -> None:
        self._audit_log = audit_log
        self._read_target = read_target or self._pick_read_target()
        self._net_target = net_target or self._DEFAULT_NET_TARGET

    # ------------------------------------------------------------------
    # Individual probes
    # ------------------------------------------------------------------

    def probe_read(self) -> ProbeResult:
        """Probe filesystem access.

        Tries to ``stat()`` the read target.  If a ``PermissionError`` or
        ``OSError`` with "access denied" (case-insensitive) is raised, the
        sandbox is blocking as expected (``passed=True``).  If the stat
        succeeds, the sandbox is not enforcing restrictions (``passed=False``).

        Returns:
            ProbeResult with name "read_probe".
        """
        name = "read_probe"
        expected = "BLOCKED"
        try:
            Path(self._read_target).stat()
            # Stat succeeded — sandbox did NOT block the access.
            actual = "ALLOWED"
            passed = False
        except PermissionError:
            actual = "BLOCKED"
            passed = True
        except OSError as exc:
            if "access denied" in str(exc).lower():
                actual = "BLOCKED"
                passed = True
            else:
                # Different OSError (e.g. file not found) — not a sandbox
                # block; treat as inconclusive / not blocking.
                actual = f"ERROR:{exc}"
                passed = False
        return ProbeResult(name=name, expected=expected, actual=actual, passed=passed)

    def probe_net(self) -> ProbeResult:
        """Probe outbound network access.

        Tries an HTTP request to the net target.  If a
        ``ConnectionRefusedError`` or ``OSError`` is raised, the sandbox is
        blocking as expected (``passed=True``).  If a response is received,
        the sandbox is not enforcing restrictions (``passed=False``).

        Returns:
            ProbeResult with name "net_probe".
        """
        name = "net_probe"
        expected = "BLOCKED"
        try:
            urllib.request.urlopen(self._net_target, timeout=0.5)
            # Got a response — sandbox did NOT block network access.
            actual = "ALLOWED"
            passed = False
        except ConnectionRefusedError:
            actual = "BLOCKED"
            passed = True
        except OSError:
            actual = "BLOCKED"
            passed = True
        except Exception as exc:  # noqa: BLE001
            # Timeouts and other non-OSError failures also indicate blocking.
            actual = f"ERROR:{exc}"
            passed = True
        return ProbeResult(name=name, expected=expected, actual=actual, passed=passed)

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------

    def run_all(self, sandbox_mode: bool = False) -> list[ProbeResult]:
        """Run all probes and return results.

        In *dev mode* (``sandbox_mode=False``), failed probes are only
        logged as informational — they do **not** indicate a security
        violation because the sandbox is not expected to be active.

        In *sandbox mode* (``sandbox_mode=True``), any probe that returns
        ``passed=False`` means the sandbox is failing to enforce its
        restrictions.  The caller is responsible for triggering SAFE_MODE.

        Args:
            sandbox_mode: Whether the process is running inside an active
                sandbox.  Affects the severity of failures in the audit log.

        Returns:
            List of :class:`ProbeResult` for every probe that was run.
        """
        results = [self.probe_read(), self.probe_net()]

        if self._audit_log is not None:
            failed = [r for r in results if not r.passed]
            event_type = "SANDBOX_PROBE_FAILURE" if failed else "SANDBOX_PROBE_OK"
            self._audit_log.write(
                event_type,
                {
                    "sandbox_mode": sandbox_mode,
                    "probes": [
                        {
                            "name": r.name,
                            "expected": r.expected,
                            "actual": r.actual,
                            "passed": r.passed,
                        }
                        for r in results
                    ],
                },
            )

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_read_target() -> str:
        """Choose the most appropriate protected path for the current OS."""
        if sys.platform == "win32":
            return "C:\\Windows\\System32\\config\\SAM"
        return "/etc/shadow"


# ---------------------------------------------------------------------------
# AttestationChecker
# ---------------------------------------------------------------------------


class AttestationChecker:
    """Validates that runtime environment matches a captured baseline.

    Captures a baseline :class:`EnvironmentFingerprint` on ``__init__``
    and compares against it on each :meth:`check` call.  Any deviation
    is considered an anomaly.

    Args:
        audit_log: Optional AuditLog to record anomaly events.
    """

    def __init__(self, audit_log: "AuditLog | None" = None) -> None:
        self._baseline = EnvironmentFingerprint.capture()
        self._audit_log = audit_log

    @property
    def baseline(self) -> EnvironmentFingerprint:
        """The baseline fingerprint captured at construction time."""
        return self._baseline

    def check(self) -> bool:
        """Compare the current environment against the baseline.

        Returns:
            True if environment matches baseline, False if an anomaly is
            detected.  On anomaly, writes an ATTESTATION_ANOMALY event to
            the audit log (if one was provided at construction time).
        """
        current = EnvironmentFingerprint.capture()

        anomalies: list[str] = []
        if current.username != self._baseline.username:
            anomalies.append(
                f"username changed: {self._baseline.username!r} -> {current.username!r}"
            )
        if current.platform != self._baseline.platform:
            anomalies.append(
                f"platform changed: {self._baseline.platform!r} -> {current.platform!r}"
            )
        if current.python_path != self._baseline.python_path:
            anomalies.append(
                f"python_path changed: {self._baseline.python_path!r} -> {current.python_path!r}"
            )
        if current.cwd != self._baseline.cwd:
            anomalies.append(
                f"cwd changed: {self._baseline.cwd!r} -> {current.cwd!r}"
            )
        if current.uid != self._baseline.uid:
            anomalies.append(
                f"uid changed: {self._baseline.uid!r} -> {current.uid!r}"
            )

        if anomalies:
            if self._audit_log is not None:
                self._audit_log.write("ATTESTATION_ANOMALY", {
                    "anomalies": anomalies,
                    "baseline": {
                        "username": self._baseline.username,
                        "platform": self._baseline.platform,
                        "python_path": self._baseline.python_path,
                        "cwd": self._baseline.cwd,
                        "uid": self._baseline.uid,
                    },
                    "current": {
                        "username": current.username,
                        "platform": current.platform,
                        "python_path": current.python_path,
                        "cwd": current.cwd,
                        "uid": current.uid,
                    },
                })
            return False

        return True
