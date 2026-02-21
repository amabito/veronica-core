#!/usr/bin/env python3
"""Sign a VERONICA policy file with Ed25519 (PolicySignerV2).

Usage:
    # From environment variable (CI / secure secret injection):
    VERONICA_PRIVATE_KEY_PEM="$(cat private_key.pem)" python tools/release_sign_policy.py

    # From key file:
    python tools/release_sign_policy.py --key-file /path/to/private_key.pem

    # Dry-run (validate args without writing):
    python tools/release_sign_policy.py --key-file /path/to/private_key.pem --dry-run

Exit codes:
    0 — success (or dry-run success)
    1 — error (missing key, signing failure, etc.)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Allow running the script directly (outside pytest) by adding src/ to path.
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
DEFAULT_POLICY = ROOT / "policies" / "default.yaml"


def load_private_key_pem(key_file: str | None) -> bytes | None:
    """Load private key PEM from env var or --key-file.

    Resolution order:
    1. VERONICA_PRIVATE_KEY_PEM environment variable.
    2. --key-file argument (path to PEM file on disk).

    Args:
        key_file: Path string from --key-file argument, or None.

    Returns:
        PEM bytes, or None if no source provided.
    """
    env_pem = os.environ.get("VERONICA_PRIVATE_KEY_PEM", "").strip()
    if env_pem:
        return env_pem.encode("utf-8")

    if key_file:
        path = Path(key_file)
        if not path.exists():
            print(f"[ERROR] Key file not found: {path}", file=sys.stderr)
            return None
        return path.read_bytes()

    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign a VERONICA policy file with Ed25519 (PolicySignerV2)."
    )
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY),
        help=f"Path to policy YAML to sign (default: {DEFAULT_POLICY})",
    )
    parser.add_argument(
        "--key-file",
        default=None,
        help="Path to PEM-encoded Ed25519 private key file. "
             "Overridden by VERONICA_PRIVATE_KEY_PEM env var.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and key loading without writing the .sig.v2 file.",
    )
    args = parser.parse_args()

    policy_path = Path(args.policy)
    if not policy_path.exists():
        print(f"[ERROR] Policy file not found: {policy_path}", file=sys.stderr)
        return 1

    private_key_pem = load_private_key_pem(args.key_file)
    if private_key_pem is None:
        print(
            "[ERROR] No private key provided. "
            "Set VERONICA_PRIVATE_KEY_PEM env var or use --key-file.",
            file=sys.stderr,
        )
        return 1

    # Import here so the tool fails fast with a useful message if missing.
    try:
        from veronica_core.security.policy_signing import PolicySignerV2
    except ImportError as exc:
        print(f"[ERROR] Cannot import PolicySignerV2: {exc}", file=sys.stderr)
        return 1

    signer = PolicySignerV2()

    if not signer.is_available():
        print(
            "[ERROR] 'cryptography' package is not installed. "
            "Install it with: pip install cryptography",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"[DRY-RUN] Would sign: {policy_path}")
        print("[DRY-RUN] Private key loaded successfully.")
        print("[DRY-RUN] No files written.")
        return 0

    try:
        signer.sign(policy_path, private_key_pem)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Signing failed: {exc}", file=sys.stderr)
        return 1

    sig_path = Path(str(policy_path) + ".sig.v2")
    print(f"[OK] Policy signed: {policy_path}")
    print(f"[OK] Signature written: {sig_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
