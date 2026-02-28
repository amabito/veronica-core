"""Tests for G-1: Policy Tamper Resistance — HMAC-SHA256 signing."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

import pytest

from veronica_core.security.policy_signing import PolicySigner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_policy(tmp_path: Path) -> Path:
    """Write a minimal YAML policy to a temp file."""
    p = tmp_path / "test_policy.yaml"
    p.write_text("version: '1.0'\ndefault: DENY\n", encoding="utf-8")
    return p


@pytest.fixture()
def signer() -> PolicySigner:
    """Return a PolicySigner with the built-in test key."""
    return PolicySigner(key=hashlib.sha256(b"veronica-dev-key").digest())


# ---------------------------------------------------------------------------
# Test 1: valid signature → verify returns True
# ---------------------------------------------------------------------------


def test_valid_sig_returns_true(tmp_policy: Path, signer: PolicySigner) -> None:
    sig = signer.sign(tmp_policy)
    sig_path = Path(str(tmp_policy) + ".sig")
    sig_path.write_text(sig + "\n", encoding="utf-8")

    assert signer.verify(tmp_policy, sig_path) is True


# ---------------------------------------------------------------------------
# Test 2: tampered YAML → verify returns False
# ---------------------------------------------------------------------------


def test_tampered_yaml_returns_false(tmp_policy: Path, signer: PolicySigner) -> None:
    sig = signer.sign(tmp_policy)
    sig_path = Path(str(tmp_policy) + ".sig")
    sig_path.write_text(sig + "\n", encoding="utf-8")

    # Tamper with the policy file after signing
    tmp_policy.write_text("version: '1.0'\ndefault: ALLOW\n", encoding="utf-8")

    assert signer.verify(tmp_policy, sig_path) is False


# ---------------------------------------------------------------------------
# Test 3: tampered sig file → verify returns False
# ---------------------------------------------------------------------------


def test_tampered_sig_returns_false(tmp_policy: Path, signer: PolicySigner) -> None:
    sig = signer.sign(tmp_policy)
    sig_path = Path(str(tmp_policy) + ".sig")
    # Write an incorrect (all-zeros) hex signature of the correct length
    bad_sig = "0" * len(sig)
    sig_path.write_text(bad_sig + "\n", encoding="utf-8")

    assert signer.verify(tmp_policy, sig_path) is False


# ---------------------------------------------------------------------------
# Test 4: missing sig file → verify returns False (no crash)
# ---------------------------------------------------------------------------


def test_missing_sig_file_returns_false(tmp_policy: Path, signer: PolicySigner) -> None:
    missing = Path(str(tmp_policy) + ".sig")
    assert not missing.exists()
    assert signer.verify(tmp_policy, missing) is False


# ---------------------------------------------------------------------------
# Test 5: VERONICA_POLICY_KEY env var overrides key
# ---------------------------------------------------------------------------


def test_env_var_overrides_key(tmp_policy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_key = b"\xde\xad\xbe\xef" * 8  # 32-byte custom key
    monkeypatch.setenv("VERONICA_POLICY_KEY", custom_key.hex())

    signer_env = PolicySigner()
    sig = signer_env.sign(tmp_policy)

    sig_path = Path(str(tmp_policy) + ".sig")
    sig_path.write_text(sig + "\n", encoding="utf-8")

    assert signer_env.verify(tmp_policy, sig_path) is True

    # Signature produced with env-key should NOT verify with default test key
    default_signer = PolicySigner(key=hashlib.sha256(b"veronica-dev-key").digest())
    assert default_signer.verify(tmp_policy, sig_path) is False


# ---------------------------------------------------------------------------
# Test 6: PolicyEngine with valid sig → loads without error
# ---------------------------------------------------------------------------


def test_policy_engine_valid_sig_ok(
    tmp_policy: Path, signer: PolicySigner, monkeypatch: pytest.MonkeyPatch
) -> None:
    sig = signer.sign(tmp_policy)
    sig_path = Path(str(tmp_policy) + ".sig")
    sig_path.write_text(sig + "\n", encoding="utf-8")

    # PolicyEngine internally creates PolicySigner() which needs a key.
    dev_key = hashlib.sha256(b"veronica-dev-key").digest()
    monkeypatch.setenv("VERONICA_POLICY_KEY", dev_key.hex())

    from veronica_core.security.policy_engine import PolicyEngine

    # Should not raise
    engine = PolicyEngine(policy_path=tmp_policy)
    assert engine is not None


# ---------------------------------------------------------------------------
# Test 7: PolicyEngine with invalid sig → raises RuntimeError
# ---------------------------------------------------------------------------


def test_policy_engine_invalid_sig_raises(
    tmp_policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sig_path = Path(str(tmp_policy) + ".sig")
    sig_path.write_text("0" * 64 + "\n", encoding="utf-8")

    # PolicyEngine internally creates PolicySigner() which needs a key
    # in non-DEV environments. Provide the dev key via env var.
    dev_key = hashlib.sha256(b"veronica-dev-key").digest()
    monkeypatch.setenv("VERONICA_POLICY_KEY", dev_key.hex())

    from veronica_core.security.policy_engine import PolicyEngine

    with pytest.raises(RuntimeError, match="Policy tamper detected"):
        PolicyEngine(policy_path=tmp_policy)


# ---------------------------------------------------------------------------
# Test 8: PolicyEngine with missing sig
#   DEV mode  → logs warning, loads OK (backward compat)
#   CI mode   → raises RuntimeError (J-1: signature required in CI)
# ---------------------------------------------------------------------------


def test_policy_engine_missing_sig_loads_ok(
    tmp_policy: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force DEV security level so the missing-sig path logs instead of raising.
    monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "DEV")

    from veronica_core.security import security_level as _sl
    from veronica_core.security.policy_engine import PolicyEngine
    import logging

    _sl.reset_security_level()
    try:
        with caplog.at_level(logging.WARNING, logger="veronica_core.security.policy_engine"):
            engine = PolicyEngine(policy_path=tmp_policy)
    finally:
        _sl.reset_security_level()

    assert engine is not None
    assert any("policy_sig_missing" in r.message for r in caplog.records)


def test_policy_engine_missing_sig_raises_in_ci(
    tmp_policy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In CI security level, missing signature must raise RuntimeError.
    monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "CI")

    from veronica_core.security import security_level as _sl
    from veronica_core.security.policy_engine import PolicyEngine

    # Reset the cached level so the env var is picked up fresh.
    _sl.reset_security_level()
    try:
        with pytest.raises(RuntimeError, match="missing"):
            PolicyEngine(policy_path=tmp_policy)
    finally:
        _sl.reset_security_level()


# ---------------------------------------------------------------------------
# Test 9: SAFE_MODE triggered on tamper (RuntimeError propagates)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 10: default.yaml + default.yaml.sig in repo verifies correctly
# ---------------------------------------------------------------------------


def test_default_policy_file_verifies(tmp_path: Path) -> None:
    """The committed default.yaml.sig must validate against default.yaml."""
    repo_root = Path(__file__).parents[2]  # veronica-core/
    policy_path = repo_root / "policies" / "default.yaml"
    sig_path = repo_root / "policies" / "default.yaml.sig"

    if not policy_path.exists() or not sig_path.exists():
        pytest.skip("policies/default.yaml or .sig not found")

    signer = PolicySigner(key=hashlib.sha256(b"veronica-dev-key").digest())
    assert signer.verify(policy_path, sig_path), (
        "policies/default.yaml.sig does not match default.yaml with the test key"
    )
