"""Runtime policy integrity monitor for VERONICA.

Periodically re-verifies PolicyBundle content_hash during runtime,
not just at startup. Uses hmac.compare_digest for timing-safe comparison.
"""

from __future__ import annotations

import hmac
import threading
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.policy.bundle import PolicyBundle


class IntegrityVerdict(Enum):
    """Result of an integrity verification check."""

    CLEAN = "CLEAN"
    TAMPERED = "TAMPERED"
    QUARANTINED = "QUARANTINED"


class IntegrityMonitor:
    """Runtime policy integrity checker.

    Captures content_hash at construction and re-verifies every
    check_interval calls to on_call(). Once TAMPERED is detected,
    sets quarantine flag -- all subsequent calls return QUARANTINED
    without re-checking.

    Thread-safe: all mutable state protected by threading.Lock.
    """

    def __init__(self, bundle: "PolicyBundle", check_interval: int = 100) -> None:
        if check_interval < 1:
            raise ValueError("check_interval must be >= 1")
        self._bundle = bundle
        self._original_hash: str = bundle.content_hash()
        self._check_interval = check_interval
        self._call_count: int = 0
        self._quarantined: bool = False
        self._lock = threading.Lock()

    def on_call(self) -> IntegrityVerdict:
        """Called on each wrap_llm_call invocation.

        Increments internal counter. Triggers full verification every
        check_interval calls. Returns CLEAN, TAMPERED, or QUARANTINED.
        """
        with self._lock:
            if self._quarantined:
                return IntegrityVerdict.QUARANTINED
            self._call_count += 1
            if self._call_count % self._check_interval == 0:
                return self._verify_locked()
            return IntegrityVerdict.CLEAN

    def force_verify(self) -> IntegrityVerdict:
        """Manual verification trigger -- bypasses call count check."""
        with self._lock:
            if self._quarantined:
                return IntegrityVerdict.QUARANTINED
            return self._verify_locked()

    def _verify_locked(self) -> IntegrityVerdict:
        """Re-compute content_hash and compare. Must be called with lock held."""
        current = self._bundle.content_hash()
        if hmac.compare_digest(current, self._original_hash):
            return IntegrityVerdict.CLEAN
        self._quarantined = True
        return IntegrityVerdict.TAMPERED

    def _verify(self) -> IntegrityVerdict:
        """Re-compute content_hash with lock acquisition."""
        with self._lock:
            return self._verify_locked()

    @property
    def is_quarantined(self) -> bool:
        """True if tamper has been detected."""
        with self._lock:
            return self._quarantined

    @property
    def call_count(self) -> int:
        """Total calls processed."""
        with self._lock:
            return self._call_count
