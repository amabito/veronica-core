"""Runner attestation for VERONICA Security Containment Layer.

Captures an environment fingerprint at startup and detects anomalies
(e.g. user switching, working directory changes) at runtime.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
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
