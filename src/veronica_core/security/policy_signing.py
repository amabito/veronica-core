"""Policy tamper resistance via HMAC-SHA256 signing.

Provides PolicySigner which can sign and verify YAML policy files
using stdlib hmac + hashlib only (zero external dependencies).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_KEY_VAR = "VERONICA_POLICY_KEY"


def _derive_test_key() -> bytes:
    """Return SHA256(b'veronica-dev-key') as the built-in test key."""
    return hashlib.sha256(b"veronica-dev-key").digest()


def _load_key() -> bytes:
    """Return signing key from env var (hex) or the built-in test key."""
    hex_key = os.environ.get(_ENV_KEY_VAR)
    if hex_key:
        return bytes.fromhex(hex_key)
    return _derive_test_key()


class PolicySigner:
    """Signs and verifies policy files with HMAC-SHA256.

    Args:
        key: Raw signing key bytes. If None, the key is loaded from
             ``VERONICA_POLICY_KEY`` env var (hex-encoded) or derived
             from the built-in test key.
    """

    def __init__(self, key: bytes | None = None) -> None:
        self._key: bytes = key if key is not None else _load_key()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sign(self, policy_path: Path) -> str:
        """Return hex-encoded HMAC-SHA256 of *policy_path* content.

        Args:
            policy_path: Path to the YAML policy file to sign.

        Returns:
            Hex string of the HMAC-SHA256 digest.
        """
        content = policy_path.read_bytes()
        mac = hmac.new(self._key, content, hashlib.sha256)
        return mac.hexdigest()

    def verify(self, policy_path: Path, sig_path: Path) -> bool:
        """Compare stored signature against freshly computed HMAC.

        Args:
            policy_path: Path to the YAML policy file.
            sig_path: Path to the ``.sig`` file containing the hex digest.

        Returns:
            True if the signature matches, False otherwise.
            Returns False if either file cannot be read.
        """
        try:
            content = policy_path.read_bytes()
            stored_sig = sig_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False

        expected = hmac.new(self._key, content, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, stored_sig)
