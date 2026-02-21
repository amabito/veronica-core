"""Policy tamper resistance via HMAC-SHA256 signing (v1) and ed25519 signing (v2).

PolicySigner  — HMAC-SHA256 using stdlib only (zero extra dependencies).
PolicySignerV2 — ed25519 using the 'cryptography' package (conditional import).

If 'cryptography' is not installed, _ED25519_AVAILABLE is False and
PolicySignerV2 raises RuntimeError on any operation that requires the key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_KEY_VAR = "VERONICA_POLICY_KEY"

# ---------------------------------------------------------------------------
# Conditional import: ed25519 via cryptography package
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_pem_public_key,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ED25519_AVAILABLE = False


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


# ---------------------------------------------------------------------------
# PolicySignerV2 — ed25519 asymmetric signing
# ---------------------------------------------------------------------------

_DEFAULT_PUBLIC_KEY_PATH = Path(__file__).parents[4] / "policies" / "public_key.pem"
_SIG_V2_SUFFIX = ".sig.v2"


class PolicySignerV2:
    """Signs and verifies policy files using ed25519 (asymmetric).

    Requires the 'cryptography' package.  If it is not installed,
    ``_ED25519_AVAILABLE`` is False and all methods raise ``RuntimeError``.

    The public key is loaded from *public_key_path* (defaults to
    ``policies/public_key.pem`` relative to the repository root).
    The private key is never stored on disk in production; it is passed
    in-memory to ``sign()``.

    Args:
        public_key_path: Path to the PEM-encoded ed25519 public key.
                         Defaults to ``policies/public_key.pem``.
    """

    def __init__(self, public_key_path: Path | None = None) -> None:
        self._public_key_path = public_key_path or _DEFAULT_PUBLIC_KEY_PATH

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """Return 'ed25519' if available, else 'unavailable'."""
        return "ed25519" if _ED25519_AVAILABLE else "unavailable"

    @staticmethod
    def is_available() -> bool:
        """Return True if the 'cryptography' package is installed."""
        return _ED25519_AVAILABLE

    # ------------------------------------------------------------------
    # Key generation (dev / CI use only)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_dev_keypair() -> tuple[bytes, bytes]:
        """Generate a fresh ed25519 keypair for development / testing.

        Returns:
            (private_key_pem, public_key_pem) as bytes.

        Raises:
            RuntimeError: If 'cryptography' is not installed.
        """
        if not _ED25519_AVAILABLE:
            raise RuntimeError(
                "cryptography package is required for ed25519 signing. "
                "Install it with: pip install cryptography"
            )
        key = Ed25519PrivateKey.generate()
        priv_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        pub_pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        return priv_pem, pub_pem

    # ------------------------------------------------------------------
    # Sign
    # ------------------------------------------------------------------

    def sign(self, policy_path: Path, private_key_pem: bytes) -> bytes:
        """Sign *policy_path* with the given private key.

        The base64-encoded signature is written to
        ``<policy_path>.sig.v2`` and also returned as raw bytes.

        Args:
            policy_path: Path to the YAML policy file.
            private_key_pem: PEM-encoded ed25519 private key bytes.

        Returns:
            Raw 64-byte ed25519 signature.

        Raises:
            RuntimeError: If 'cryptography' is not installed.
        """
        if not _ED25519_AVAILABLE:
            raise RuntimeError(
                "cryptography package is required for ed25519 signing."
            )
        content = policy_path.read_bytes()
        private_key = load_pem_private_key(private_key_pem, password=None)
        raw_sig: bytes = private_key.sign(content)  # type: ignore[attr-defined]

        sig_path = Path(str(policy_path) + _SIG_V2_SUFFIX)
        sig_path.write_text(base64.b64encode(raw_sig).decode("ascii") + "\n", encoding="utf-8")

        return raw_sig

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(self, policy_path: Path, sig_path: Path) -> bool:
        """Verify the ed25519 signature stored in *sig_path*.

        Args:
            policy_path: Path to the YAML policy file.
            sig_path: Path to the ``.sig.v2`` file (base64-encoded sig).

        Returns:
            True if the signature is valid, False otherwise.
        """
        if not _ED25519_AVAILABLE:
            logger.warning(
                "policy_signing_v2: cryptography not available; cannot verify ed25519 sig"
            )
            return False

        try:
            content = policy_path.read_bytes()
            sig_b64 = sig_path.read_text(encoding="utf-8").strip()
            raw_sig = base64.b64decode(sig_b64)
            pub_pem = self._public_key_path.read_bytes()
        except OSError:
            return False
        except Exception:
            return False

        try:
            public_key = load_pem_public_key(pub_pem)
            public_key.verify(raw_sig, content)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
