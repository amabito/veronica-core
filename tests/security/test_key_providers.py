"""Tests for KeyProvider abstractions."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from veronica_core.security.key_providers import (
    EnvKeyProvider,
    FileKeyProvider,
    KeyProvider,
    VaultKeyProvider,
    _VAULT_AVAILABLE,
)
from veronica_core.security.policy_signing import (
    PolicySignerV2,
    _ED25519_AVAILABLE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PEM = b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VdAyEAtest\n-----END PUBLIC KEY-----\n"


@pytest.fixture()
def pem_file(tmp_path: Path) -> Path:
    p = tmp_path / "public_key.pem"
    p.write_bytes(SAMPLE_PEM)
    return p


@pytest.fixture()
def dev_keypair() -> tuple[bytes, bytes]:
    if not _ED25519_AVAILABLE:
        pytest.skip("cryptography not installed")
    return PolicySignerV2.generate_dev_keypair()


@pytest.fixture()
def tmp_policy(tmp_path: Path) -> Path:
    p = tmp_path / "test_policy.yaml"
    p.write_text("version: '1.0'\ndefault: DENY\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestFileKeyProvider
# ---------------------------------------------------------------------------

class TestFileKeyProvider:
    def test_read_success(self, pem_file: Path) -> None:
        provider = FileKeyProvider(pem_file)
        assert provider.get_public_key_pem() == SAMPLE_PEM

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        provider = FileKeyProvider(tmp_path / "nonexistent.pem")
        with pytest.raises(FileNotFoundError):
            provider.get_public_key_pem()

    def test_satisfies_protocol(self, pem_file: Path) -> None:
        provider = FileKeyProvider(pem_file)
        assert isinstance(provider, KeyProvider)


# ---------------------------------------------------------------------------
# TestEnvKeyProvider
# ---------------------------------------------------------------------------

class TestEnvKeyProvider:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----\n")
        provider = EnvKeyProvider()
        result = provider.get_public_key_pem()
        assert result == b"-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----\n"

    def test_custom_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_KEY", "custom_pem_content")
        provider = EnvKeyProvider(env_var="MY_CUSTOM_KEY")
        assert provider.get_public_key_pem() == b"custom_pem_content"

    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VERONICA_PUBLIC_KEY_PEM", raising=False)
        provider = EnvKeyProvider()
        with pytest.raises(RuntimeError, match="VERONICA_PUBLIC_KEY_PEM"):
            provider.get_public_key_pem()

    def test_missing_custom_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_KEY", raising=False)
        provider = EnvKeyProvider(env_var="MY_KEY")
        with pytest.raises(RuntimeError, match="MY_KEY"):
            provider.get_public_key_pem()

    def test_satisfies_protocol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", "pem")
        provider = EnvKeyProvider()
        assert isinstance(provider, KeyProvider)


# ---------------------------------------------------------------------------
# TestVaultKeyProvider
# ---------------------------------------------------------------------------

class TestVaultKeyProvider:
    def test_raises_without_hvac(self) -> None:
        with patch("veronica_core.security.key_providers._VAULT_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="hvac is required"):
                VaultKeyProvider(vault_url="https://vault.example.com")

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_mock_hvac_success(self) -> None:
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {
            "data": {
                "keys": {
                    "1": {"public_key": "-----BEGIN PUBLIC KEY-----\nvault_key\n-----END PUBLIC KEY-----\n"},
                }
            }
        }
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(
                vault_url="https://vault.example.com",
                key_name="veronica-policy",
            )
            result = provider.get_public_key_pem()
        assert result == b"-----BEGIN PUBLIC KEY-----\nvault_key\n-----END PUBLIC KEY-----\n"

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_vault_connection_failure_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.side_effect = ConnectionError("connection refused")
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises(RuntimeError, match="Failed to fetch key"):
                provider.get_public_key_pem()

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_vault_empty_keys_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {"data": {"keys": {}}}
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises(RuntimeError, match="No keys found"):
                provider.get_public_key_pem()

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_vault_missing_public_key_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {
            "data": {"keys": {"1": {}}}
        }
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises(RuntimeError, match="No public key found"):
                provider.get_public_key_pem()

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_vault_uses_latest_version(self) -> None:
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {
            "data": {
                "keys": {
                    "1": {"public_key": "old_key"},
                    "2": {"public_key": "latest_key"},
                }
            }
        }
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            result = provider.get_public_key_pem()
        assert result == b"latest_key"

    @pytest.mark.skipif(not _VAULT_AVAILABLE, reason="hvac not installed")
    def test_vault_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_TOKEN", "my-token")
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {
            "data": {"keys": {"1": {"public_key": "pem"}}}
        }
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            provider.get_public_key_pem()
            mock_hvac.Client.assert_called_once_with(
                url="https://vault.example.com",
                token="my-token",
                namespace=None,
            )


# ---------------------------------------------------------------------------
# TestKeyProviderProtocol — custom class satisfies Protocol
# ---------------------------------------------------------------------------

class TestKeyProviderProtocol:
    def test_custom_class_satisfies_protocol(self) -> None:
        class MyKeyProvider:
            def get_public_key_pem(self) -> bytes:
                return b"custom_pem"

        provider = MyKeyProvider()
        assert isinstance(provider, KeyProvider)

    def test_class_without_method_does_not_satisfy_protocol(self) -> None:
        class NotAKeyProvider:
            pass

        obj = NotAKeyProvider()
        assert not isinstance(obj, KeyProvider)


# ---------------------------------------------------------------------------
# TestPolicySignerV2WithKeyProvider
# ---------------------------------------------------------------------------

class TestPolicySignerV2WithKeyProvider:
    def test_verify_via_env_key_provider(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        priv_pem, pub_pem = dev_keypair

        # Write pub key to file for signing (need path for sign())
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)
        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        # Now verify via EnvKeyProvider
        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", pub_pem.decode("utf-8"))
        env_provider = EnvKeyProvider()
        signer_via_env = PolicySignerV2(key_provider=env_provider)
        assert signer_via_env.verify(tmp_policy, sig_path) is True

    def test_verify_via_file_key_provider(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        file_provider = FileKeyProvider(pub_path)
        signer_via_file = PolicySignerV2(key_provider=file_provider)
        assert signer_via_file.verify(tmp_policy, sig_path) is True

    def test_default_init_uses_file_provider(self, tmp_path: Path) -> None:
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(SAMPLE_PEM)
        signer = PolicySignerV2(public_key_path=pub_path)
        # Should have a key_provider attribute (FileKeyProvider)
        from veronica_core.security.key_providers import FileKeyProvider as FKP
        assert isinstance(signer._key_provider, FKP)

    def test_key_provider_overrides_file_path_for_verify(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        priv_pem, pub_pem = dev_keypair

        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)
        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        # Wrong file path but correct key_provider — should verify OK
        wrong_path = tmp_path / "wrong.pem"
        wrong_path.write_bytes(b"not_a_real_key")
        correct_provider = FileKeyProvider(pub_path)
        signer_override = PolicySignerV2(
            public_key_path=wrong_path,
            key_provider=correct_provider,
        )
        assert signer_override.verify(tmp_policy, sig_path) is True

    def test_env_provider_missing_var_causes_verify_false(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)
        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        monkeypatch.delenv("VERONICA_PUBLIC_KEY_PEM", raising=False)
        env_provider = EnvKeyProvider()
        signer_bad = PolicySignerV2(key_provider=env_provider)
        # RuntimeError from provider is caught and returns False
        assert signer_bad.verify(tmp_policy, sig_path) is False


# ---------------------------------------------------------------------------
# TestPolicyEngineWithKeyProvider
# ---------------------------------------------------------------------------

class TestPolicyEngineWithKeyProvider:
    def test_init_accepts_key_provider(self) -> None:
        from veronica_core.security.policy_engine import PolicyEngine

        provider = FileKeyProvider(Path("nonexistent.pem"))
        engine = PolicyEngine(key_provider=provider)
        assert engine._key_provider is provider

    def test_init_without_key_provider_is_none(self) -> None:
        from veronica_core.security.policy_engine import PolicyEngine

        engine = PolicyEngine()
        assert engine._key_provider is None

    def test_policy_engine_with_env_key_provider(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hashlib

        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        # Sign the policy
        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)

        # Pin the public key
        pin = hashlib.sha256(pub_pem.strip()).hexdigest()
        monkeypatch.setenv("VERONICA_KEY_PIN", pin)

        from veronica_core.security.policy_engine import PolicyEngine

        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", pub_pem.decode("utf-8"))
        env_provider = EnvKeyProvider()

        engine = PolicyEngine(
            policy_path=tmp_policy,
            key_provider=env_provider,
        )
        assert engine is not None


# ---------------------------------------------------------------------------
# TestAdversarialKeyProvider — attacker mindset
# ---------------------------------------------------------------------------


class TestAdversarialKeyProvider:
    """Adversarial tests for KeyProvider — corrupted input, concurrency, boundary abuse."""

    # -- Corrupted input --

    def test_file_provider_empty_file_returns_empty_bytes(self, tmp_path: Path) -> None:
        """Empty PEM file should return b'' (caller decides validity)."""
        empty = tmp_path / "empty.pem"
        empty.write_bytes(b"")
        provider = FileKeyProvider(empty)
        assert provider.get_public_key_pem() == b""

    def test_file_provider_binary_garbage(self, tmp_path: Path) -> None:
        """Binary garbage in PEM file should be returned as-is (raw bytes)."""
        garbage = tmp_path / "garbage.pem"
        garbage.write_bytes(b"\x00\xff\xfe\xfd" * 100)
        provider = FileKeyProvider(garbage)
        result = provider.get_public_key_pem()
        assert result == b"\x00\xff\xfe\xfd" * 100

    def test_env_provider_empty_string_returns_empty_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty env var value should return b'' not raise."""
        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", "")
        provider = EnvKeyProvider()
        assert provider.get_public_key_pem() == b""

    def test_env_provider_non_ascii_utf8(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-ASCII UTF-8 in env var should encode correctly."""
        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", "日本語キー")
        provider = EnvKeyProvider()
        result = provider.get_public_key_pem()
        assert result == "日本語キー".encode("utf-8")

    def test_file_provider_permission_denied(self, tmp_path: Path) -> None:
        """Unreadable file should raise OSError."""
        import os
        import sys

        if sys.platform == "win32":
            pytest.skip("chmod not reliable on Windows")
        secret = tmp_path / "noperm.pem"
        secret.write_bytes(b"secret")
        os.chmod(secret, 0o000)
        try:
            provider = FileKeyProvider(secret)
            with pytest.raises(PermissionError):
                provider.get_public_key_pem()
        finally:
            os.chmod(secret, 0o644)

    # -- Concurrent access --

    def test_file_provider_concurrent_reads(self, pem_file: Path) -> None:
        """Multiple threads reading same file should not corrupt or crash."""
        import threading

        provider = FileKeyProvider(pem_file)
        results: list[bytes] = []
        errors: list[Exception] = []

        def read() -> None:
            try:
                results.append(provider.get_public_key_pem())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent read errors: {errors}"
        assert len(results) == 20
        assert all(r == SAMPLE_PEM for r in results)

    def test_env_provider_concurrent_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple threads reading same env var should be thread-safe."""
        import threading

        monkeypatch.setenv("VERONICA_PUBLIC_KEY_PEM", "concurrent_test_pem")
        provider = EnvKeyProvider()
        results: list[bytes] = []
        errors: list[Exception] = []

        def read() -> None:
            try:
                results.append(provider.get_public_key_pem())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 20
        assert all(r == b"concurrent_test_pem" for r in results)

    # -- Boundary abuse --

    def test_file_provider_large_pem_file(self, tmp_path: Path) -> None:
        """Large PEM file (1MB) should be read without issue."""
        large = tmp_path / "large.pem"
        content = b"-----BEGIN PUBLIC KEY-----\n" + b"A" * (1024 * 1024) + b"\n-----END PUBLIC KEY-----\n"
        large.write_bytes(content)
        provider = FileKeyProvider(large)
        result = provider.get_public_key_pem()
        assert len(result) > 1024 * 1024

    def test_vault_provider_response_keys_is_list_not_dict(self) -> None:
        """Vault returns keys as list instead of dict -- should raise, not crash."""
        if not _VAULT_AVAILABLE:
            pytest.skip("hvac not installed")
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {
            "data": {"keys": ["not", "a", "dict"]}
        }
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises((RuntimeError, TypeError, AttributeError)):
                provider.get_public_key_pem()

    def test_vault_provider_response_data_is_none(self) -> None:
        """Vault returns None data -- should raise RuntimeError."""
        if not _VAULT_AVAILABLE:
            pytest.skip("hvac not installed")
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {"data": None}
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises((RuntimeError, TypeError, AttributeError)):
                provider.get_public_key_pem()

    def test_vault_provider_response_entirely_empty(self) -> None:
        """Vault returns empty dict -- should raise RuntimeError."""
        if not _VAULT_AVAILABLE:
            pytest.skip("hvac not installed")
        mock_client = MagicMock()
        mock_client.secrets.transit.get_key.return_value = {}
        with patch("veronica_core.security.key_providers.hvac") as mock_hvac:
            mock_hvac.Client.return_value = mock_client
            provider = VaultKeyProvider(vault_url="https://vault.example.com")
            with pytest.raises(RuntimeError, match="No keys found"):
                provider.get_public_key_pem()

    # -- PolicySignerV2 integration with corrupted key_provider --

    def test_signer_verify_with_garbage_key_returns_false(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        """key_provider returning garbage PEM should cause verify to return False, not crash."""
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        class GarbageProvider:
            def get_public_key_pem(self) -> bytes:
                return b"\x00\xff garbage not a PEM"

        bad_signer = PolicySignerV2(key_provider=GarbageProvider())
        assert bad_signer.verify(tmp_policy, sig_path) is False

    def test_signer_verify_with_wrong_key_returns_false(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        """key_provider returning a different valid key should return False."""
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        # Generate a different keypair
        _, other_pub = PolicySignerV2.generate_dev_keypair()

        class WrongKeyProvider:
            def get_public_key_pem(self) -> bytes:
                return other_pub

        wrong_signer = PolicySignerV2(key_provider=WrongKeyProvider())
        assert wrong_signer.verify(tmp_policy, sig_path) is False

    def test_signer_verify_with_raising_provider_returns_false(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        """key_provider that raises RuntimeError should cause verify to return False."""
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        class ExplodingProvider:
            def get_public_key_pem(self) -> bytes:
                raise RuntimeError("Vault connection timeout")

        exploding_signer = PolicySignerV2(key_provider=ExplodingProvider())
        assert exploding_signer.verify(tmp_policy, sig_path) is False

    # -- Exception type coverage in verify() --

    @pytest.mark.parametrize(
        "exc_type",
        [ConnectionError, KeyError, TypeError, AttributeError, UnicodeDecodeError],
        ids=["ConnectionError", "KeyError", "TypeError", "AttributeError", "UnicodeDecodeError"],
    )
    def test_signer_verify_uncaught_provider_exception_does_not_crash(
        self,
        exc_type: type,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
    ) -> None:
        """key_provider raising non-OSError/RuntimeError must not crash verify().

        The except clause at policy_signing.py:289 only catches (OSError, RuntimeError).
        Other exceptions (ConnectionError, KeyError, TypeError, etc.) from
        key_provider.get_public_key_pem() would propagate uncaught.
        This test documents whether the code handles them or leaks them.
        """
        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)
        sig_path = Path(str(tmp_policy) + ".sig.v2")

        class WeirdProvider:
            def get_public_key_pem(self) -> bytes:
                if exc_type is UnicodeDecodeError:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
                raise exc_type("simulated failure")

        weird_signer = PolicySignerV2(key_provider=WeirdProvider())
        # ConnectionError is subclass of OSError, so it's caught.
        # KeyError, TypeError, AttributeError are NOT caught by (OSError, RuntimeError).
        # UnicodeDecodeError is subclass of ValueError, caught by the ValueError handler.
        # This test verifies actual behavior — if it crashes, we need to fix the code.
        try:
            result = weird_signer.verify(tmp_policy, sig_path)
            assert result is False
        except Exception:
            # If it raises, the except clause is too narrow — flag for fix
            pytest.fail(
                f"verify() crashed when key_provider raised {exc_type.__name__}. "
                "The except clause at policy_signing.py:289 needs broadening."
            )

    # -- PolicyEngine integration chain with broken key_provider --

    def test_policy_engine_with_garbage_key_provider_raises_tamper(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PolicyEngine.__init__ with key_provider returning garbage should raise RuntimeError.

        Full chain: PolicyEngine.__init__ -> _verify_policy_signature ->
        _validate_jwk_format -> PolicySignerV2.verify() returns False ->
        raises RuntimeError('Policy tamper detected').
        """
        import hashlib

        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        # Sign the policy with correct key
        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)

        # Pin the correct key
        pin = hashlib.sha256(pub_pem.strip()).hexdigest()
        monkeypatch.setenv("VERONICA_KEY_PIN", pin)

        class GarbageProvider:
            def get_public_key_pem(self) -> bytes:
                return b"not a real PEM"

        from veronica_core.security.policy_engine import PolicyEngine

        with pytest.raises(RuntimeError, match="Policy tamper detected"):
            PolicyEngine(
                policy_path=tmp_policy,
                key_provider=GarbageProvider(),
            )

    def test_policy_engine_none_key_provider_uses_default_file(
        self,
        dev_keypair: tuple[bytes, bytes],
        tmp_path: Path,
        tmp_policy: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PolicyEngine with key_provider=None should fall back to FileKeyProvider(default_path).

        This tests the None -> FileKeyProvider fallback chain through the full
        PolicyEngine -> PolicySignerV2 integration.
        """
        import hashlib

        priv_pem, pub_pem = dev_keypair
        pub_path = tmp_path / "public_key.pem"
        pub_path.write_bytes(pub_pem)

        signer = PolicySignerV2(public_key_path=pub_path)
        signer.sign(tmp_policy, priv_pem)

        pin = hashlib.sha256(pub_pem.strip()).hexdigest()
        monkeypatch.setenv("VERONICA_KEY_PIN", pin)

        from veronica_core.security.policy_engine import PolicyEngine

        # key_provider=None, but public_key_path provided -- should use file
        engine = PolicyEngine(
            policy_path=tmp_policy,
            public_key_path=pub_path,
            key_provider=None,
        )
        assert engine is not None
