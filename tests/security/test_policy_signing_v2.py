"""Tests for Policy Signing v2 — ed25519 asymmetric signing."""
from __future__ import annotations

from pathlib import Path

import pytest

from veronica_core.security.policy_signing import (
    PolicySignerV2,
    _ED25519_AVAILABLE,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_policy(tmp_path: Path) -> Path:
    p = tmp_path / "test_policy.yaml"
    p.write_text("version: '1.0'\ndefault: DENY\n", encoding="utf-8")
    return p


@pytest.fixture()
def dev_keypair() -> tuple[bytes, bytes]:
    """Return (priv_pem, pub_pem) — skips if cryptography not available."""
    if not _ED25519_AVAILABLE:
        pytest.skip("cryptography not installed")
    return PolicySignerV2.generate_dev_keypair()


@pytest.fixture()
def signer_v2(dev_keypair: tuple[bytes, bytes], tmp_path: Path) -> tuple[PolicySignerV2, bytes]:
    """Return (signer, priv_pem) with pub key written to tmp_path."""
    priv_pem, pub_pem = dev_keypair
    pub_path = tmp_path / "public_key.pem"
    pub_path.write_bytes(pub_pem)
    return PolicySignerV2(public_key_path=pub_path), priv_pem


# ---------------------------------------------------------------------------
# Basic availability checks (always run, no skip)
# ---------------------------------------------------------------------------


def test_is_available_does_not_raise() -> None:
    """is_available() must never raise."""
    result = PolicySignerV2.is_available()
    assert isinstance(result, bool)


def test_mode_property() -> None:
    """mode returns 'ed25519' or 'unavailable' depending on install state."""
    signer = PolicySignerV2()
    assert signer.mode in ("ed25519", "unavailable")


def test_mode_matches_availability() -> None:
    """mode must be consistent with is_available()."""
    signer = PolicySignerV2()
    if _ED25519_AVAILABLE:
        assert signer.mode == "ed25519"
    else:
        assert signer.mode == "unavailable"


# ---------------------------------------------------------------------------
# Tests requiring ed25519 (skipped when cryptography unavailable)
# ---------------------------------------------------------------------------


def test_generate_dev_keypair_returns_pem(dev_keypair: tuple[bytes, bytes]) -> None:
    priv_pem, pub_pem = dev_keypair
    assert priv_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub_pem.startswith(b"-----BEGIN PUBLIC KEY-----")


def test_sign_creates_sig_v2_file(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)
    sig_path = Path(str(tmp_policy) + ".sig.v2")
    assert sig_path.exists(), ".sig.v2 file must be created by sign()"


def test_valid_sig_verifies(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)
    sig_path = Path(str(tmp_policy) + ".sig.v2")
    assert signer.verify(tmp_policy, sig_path) is True


def test_tampered_yaml_fails_verification(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)
    sig_path = Path(str(tmp_policy) + ".sig.v2")

    # Tamper the policy content after signing
    tmp_policy.write_text("version: '1.0'\ndefault: ALLOW\n", encoding="utf-8")

    assert signer.verify(tmp_policy, sig_path) is False


def test_tampered_sig_fails_verification(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)
    sig_path = Path(str(tmp_policy) + ".sig.v2")

    # Overwrite sig with garbage base64
    import base64
    sig_path.write_text(base64.b64encode(b"\x00" * 64).decode() + "\n", encoding="utf-8")

    assert signer.verify(tmp_policy, sig_path) is False


def test_missing_sig_file_returns_false(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    signer, _ = signer_v2
    missing = Path(str(tmp_policy) + ".sig.v2")
    assert not missing.exists()
    assert signer.verify(tmp_policy, missing) is False


def test_wrong_key_fails_verification(tmp_policy: Path, tmp_path: Path) -> None:
    """Signature made with key A must not verify with key B."""
    if not _ED25519_AVAILABLE:
        pytest.skip("cryptography not installed")

    priv_a, pub_a = PolicySignerV2.generate_dev_keypair()
    priv_b, pub_b = PolicySignerV2.generate_dev_keypair()

    pub_a_path = tmp_path / "pub_a.pem"
    pub_a_path.write_bytes(pub_a)
    pub_b_path = tmp_path / "pub_b.pem"
    pub_b_path.write_bytes(pub_b)

    signer_a = PolicySignerV2(public_key_path=pub_a_path)
    signer_b = PolicySignerV2(public_key_path=pub_b_path)

    # Sign with key A
    signer_a.sign(tmp_policy, priv_a)
    sig_path = Path(str(tmp_policy) + ".sig.v2")

    # Verify with key B — must fail
    assert signer_b.verify(tmp_policy, sig_path) is False


# ---------------------------------------------------------------------------
# PolicyEngine v2 integration
# ---------------------------------------------------------------------------


def test_policy_engine_v2_valid_sig_ok(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path, tmp_path: Path
) -> None:
    """PolicyEngine must load without error when .sig.v2 is valid."""
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)

    from veronica_core.security.policy_engine import PolicyEngine

    engine = PolicyEngine(policy_path=tmp_policy, public_key_path=signer._public_key_path)
    assert engine is not None


def test_policy_engine_v2_tampered_raises(
    signer_v2: tuple[PolicySignerV2, bytes], tmp_policy: Path
) -> None:
    """PolicyEngine must raise RuntimeError when .sig.v2 is tampered."""
    signer, priv_pem = signer_v2
    signer.sign(tmp_policy, priv_pem)

    # Tamper sig
    import base64
    sig_path = Path(str(tmp_policy) + ".sig.v2")
    sig_path.write_text(base64.b64encode(b"\xff" * 64).decode() + "\n", encoding="utf-8")

    from veronica_core.security.policy_engine import PolicyEngine

    with pytest.raises(RuntimeError, match="Policy tamper detected"):
        PolicyEngine(policy_path=tmp_policy, public_key_path=signer._public_key_path)


# ---------------------------------------------------------------------------
# Committed dev artifacts
# ---------------------------------------------------------------------------


def test_committed_public_key_and_sig_verify() -> None:
    """The committed public_key.pem + default.yaml.sig.v2 must verify."""
    if not _ED25519_AVAILABLE:
        pytest.skip("cryptography not installed")

    repo_root = Path(__file__).parents[2]
    policy_path = repo_root / "policies" / "default.yaml"
    pub_key_path = repo_root / "policies" / "public_key.pem"
    sig_v2_path = repo_root / "policies" / "default.yaml.sig.v2"

    if not policy_path.exists() or not pub_key_path.exists() or not sig_v2_path.exists():
        pytest.skip("Required files not found in policies/")

    signer = PolicySignerV2(public_key_path=pub_key_path)
    assert signer.verify(policy_path, sig_v2_path), (
        "policies/default.yaml.sig.v2 does not verify against public_key.pem"
    )
