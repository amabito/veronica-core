#!/usr/bin/env python3
"""Verify VERONICA release artifacts before publishing.

Checks:
  1. Signature file exists  (.sig.v2 beside the policy YAML)
  2. Key pin matches        (policies/key_pin.txt vs public_key.pem SHA-256)
  3. Ed25519 signature valid (PolicySignerV2.verify)
  4. policy_version is set  (YAML metadata field present and non-empty)

Usage:
    python tools/verify_release.py
    python tools/verify_release.py --policy policies/default.yaml

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Allow running the script directly (outside pytest) by adding src/ to path.
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
DEFAULT_POLICY = ROOT / "policies" / "default.yaml"
PUBLIC_KEY_PATH = ROOT / "policies" / "public_key.pem"
KEY_PIN_PATH = ROOT / "policies" / "key_pin.txt"


# ---------------------------------------------------------------------------
# Simple YAML field reader (no third-party dependency fallback)
# ---------------------------------------------------------------------------

def _read_policy_version_yaml(yaml_text: str) -> str | None:
    """Extract the policy_version field value from YAML text.

    Uses pyyaml when available; falls back to a line-scanning heuristic.

    Args:
        yaml_text: Raw text content of the policy YAML file.

    Returns:
        String value of policy_version, or None if not found / empty.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(yaml_text)
        if isinstance(data, dict):
            val = data.get("policy_version")
            return str(val) if val is not None else None
        return None
    except Exception:
        pass

    # Fallback: scan lines for "policy_version:" prefix.
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("policy_version:"):
            _, _, value = stripped.partition(":")
            value = value.strip().strip('"').strip("'")
            return value if value else None

    return None


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

class CheckResult:
    """Collects pass/fail results."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)
        print(f"  [PASS] {msg}")

    def fail(self, msg: str) -> None:
        self.failed.append(msg)
        print(f"  [FAIL] {msg}")

    @property
    def success(self) -> bool:
        return len(self.failed) == 0


def check_sig_file_exists(result: CheckResult, policy_path: Path) -> Path | None:
    """Check 1: .sig.v2 file exists beside policy YAML.

    Args:
        result: Accumulator for pass/fail messages.
        policy_path: Path to the policy YAML.

    Returns:
        Path to the sig file if it exists, else None.
    """
    sig_path = Path(str(policy_path) + ".sig.v2")
    if sig_path.exists():
        result.ok(f"Signature file exists: {sig_path.name}")
        return sig_path
    else:
        result.fail(f"Signature file missing: {sig_path}")
        return None


def check_key_pin(result: CheckResult) -> None:
    """Check 2: public_key.pem SHA-256 matches key_pin.txt.

    Args:
        result: Accumulator for pass/fail messages.
    """
    if not PUBLIC_KEY_PATH.exists():
        result.fail(f"Public key file missing: {PUBLIC_KEY_PATH}")
        return
    if not KEY_PIN_PATH.exists():
        result.fail(f"Key pin file missing: {KEY_PIN_PATH}")
        return

    pem_bytes = PUBLIC_KEY_PATH.read_bytes().strip()
    actual_pin = hashlib.sha256(pem_bytes).hexdigest()
    expected_pin = KEY_PIN_PATH.read_text(encoding="utf-8").strip()

    if actual_pin == expected_pin:
        result.ok(f"Key pin matches: {actual_pin[:16]}...")
    else:
        result.fail(
            f"Key pin MISMATCH: expected={expected_pin[:16]}... "
            f"actual={actual_pin[:16]}..."
        )


def check_ed25519_sig(result: CheckResult, policy_path: Path, sig_path: Path) -> None:
    """Check 3: Ed25519 signature is valid.

    Args:
        result: Accumulator for pass/fail messages.
        policy_path: Path to the policy YAML.
        sig_path: Path to the .sig.v2 file.
    """
    try:
        from veronica_core.security.policy_signing import PolicySignerV2
    except ImportError as exc:
        result.fail(f"Cannot import PolicySignerV2: {exc}")
        return

    # Pass explicit public key path so the tool works correctly regardless of
    # the install location (PolicySignerV2._DEFAULT_PUBLIC_KEY_PATH uses
    # parents[4] which breaks when running from source).
    signer = PolicySignerV2(public_key_path=PUBLIC_KEY_PATH)

    if not signer.is_available():
        result.fail(
            "Ed25519 unavailable: 'cryptography' package not installed. "
            "Install with: pip install cryptography"
        )
        return

    if signer.verify(policy_path, sig_path):
        result.ok("Ed25519 signature valid")
    else:
        result.fail("Ed25519 signature INVALID or verification failed")


def check_policy_version(result: CheckResult, policy_path: Path) -> None:
    """Check 4: policy_version field is set in the YAML.

    Args:
        result: Accumulator for pass/fail messages.
        policy_path: Path to the policy YAML.
    """
    try:
        yaml_text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.fail(f"Cannot read policy file: {exc}")
        return

    version = _read_policy_version_yaml(yaml_text)
    if version:
        result.ok(f"policy_version is set: {version}")
    else:
        result.fail("policy_version is missing or empty in policy YAML")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify VERONICA release artifacts (sig, key pin, ed25519, version)."
    )
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY),
        help=f"Path to policy YAML to verify (default: {DEFAULT_POLICY})",
    )
    args = parser.parse_args()

    policy_path = Path(args.policy)

    print(f"Verifying release artifacts for: {policy_path}")
    print("=" * 60)

    result = CheckResult()

    if not policy_path.exists():
        result.fail(f"Policy file not found: {policy_path}")
        print(f"\nResults: 0 passed, 1 failed")
        return 1

    print("\n--- Check 1: Signature file ---")
    sig_path = check_sig_file_exists(result, policy_path)

    print("\n--- Check 2: Key pin ---")
    check_key_pin(result)

    print("\n--- Check 3: Ed25519 signature ---")
    if sig_path is not None:
        check_ed25519_sig(result, policy_path, sig_path)
    else:
        result.fail("Skipped (no sig file)")

    print("\n--- Check 4: policy_version ---")
    check_policy_version(result, policy_path)

    print(f"\n{'=' * 60}")
    print(f"Results: {len(result.passed)} passed, {len(result.failed)} failed")

    if result.failed:
        print("\nFailed checks:")
        for msg in result.failed:
            print(f"  - {msg}")
        print("\nRelease verification FAILED.")
        return 1

    print("\nRelease verification PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
