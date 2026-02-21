"""Public key pinning for VERONICA containment layer (J-2).

Computes a SHA-256 hash of the ed25519 public key PEM and compares it
against a pinned value stored in an environment variable or a file.
In CI/PROD environments, a mismatch raises RuntimeError.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_PIN_VAR = "VERONICA_KEY_PIN"
_DEFAULT_PIN_FILE = Path(__file__).parents[4] / "policies" / "key_pin.txt"


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------

def compute_key_hash(pem_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of *pem_bytes* (stripped of whitespace).

    Args:
        pem_bytes: Raw bytes of the PEM-encoded public key.

    Returns:
        Lowercase hex string of the SHA-256 hash.
    """
    stripped = pem_bytes.strip()
    return hashlib.sha256(stripped).hexdigest()


# ---------------------------------------------------------------------------
# Expected pin loading
# ---------------------------------------------------------------------------

def load_expected_pin() -> str | None:
    """Return the expected key pin from env var or the default pin file.

    Resolution order:
    1. ``VERONICA_KEY_PIN`` environment variable (hex string).
    2. ``policies/key_pin.txt`` in the repository root.

    Returns:
        Hex-encoded SHA-256 pin string, or ``None`` if neither source is
        available.
    """
    env_pin = os.environ.get(_ENV_PIN_VAR, "").strip()
    if env_pin:
        return env_pin

    pin_file = _DEFAULT_PIN_FILE
    if pin_file.exists():
        try:
            return pin_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    return None


# ---------------------------------------------------------------------------
# KeyPinChecker
# ---------------------------------------------------------------------------

class KeyPinChecker:
    """Verifies that a public key PEM matches the pinned SHA-256 hash.

    Args:
        audit_log: Optional audit log instance.  If provided, a
                   ``key_pin_mismatch`` event is emitted on hash mismatch.
    """

    def __init__(self, audit_log: Any | None = None) -> None:
        self._audit_log = audit_log

    def check(self, pem_bytes: bytes) -> bool:
        """Compare *pem_bytes* hash against the pinned value.

        If no pin is configured, the check passes (non-strict behaviour).
        A mismatch is logged and an audit event is emitted.

        Args:
            pem_bytes: PEM-encoded public key bytes to check.

        Returns:
            ``True`` if the key matches the pin (or if no pin is
            configured).  ``False`` on mismatch.
        """
        expected = load_expected_pin()
        if expected is None:
            logger.debug("key_pin: no pin configured; skipping verification")
            return True

        actual = compute_key_hash(pem_bytes)
        if actual == expected:
            return True

        logger.error(
            "key_pin_mismatch: expected=%s actual=%s",
            expected,
            actual,
        )
        self._emit_audit_mismatch(expected, actual)
        return False

    def enforce(self, pem_bytes: bytes) -> None:
        """Call :meth:`check` and raise ``RuntimeError`` on mismatch in CI/PROD.

        In DEV mode, a mismatch is logged but does not raise.

        Args:
            pem_bytes: PEM-encoded public key bytes to enforce.

        Raises:
            RuntimeError: If the key pin does not match in CI or PROD
                          security level.
        """
        from veronica_core.security.security_level import SecurityLevel, get_security_level

        ok = self.check(pem_bytes)
        if not ok:
            level = get_security_level()
            if level in (SecurityLevel.CI, SecurityLevel.PROD):
                raise RuntimeError(
                    f"Key pin mismatch in {level.name} environment — "
                    "public key has changed unexpectedly. "
                    "See docs/KEY_ROTATION.md for rotation steps."
                )
            logger.warning("key_pin_mismatch in DEV mode — not raising")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_audit_mismatch(self, expected: str, actual: str) -> None:
        """Emit an audit event if an audit log is attached."""
        if self._audit_log is None:
            return
        try:
            self._audit_log.write(
                "key_pin_mismatch",
                {"expected": expected, "actual": actual},
            )
        except Exception:
            pass
