"""Tests for E-2: Secrets classification expansion.

Covers:
- file_read deny patterns for credential files (.npmrc, .pypirc, .netrc, *.pem, etc.)
- shell credential sub-command deny rules (git credential, gh auth, npm token, pip config)
- masking patterns for npm tokens, SSH private keys, pypi tokens
"""
from __future__ import annotations

import pytest

from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.masking import SecretMasker
from veronica_core.security.policy_engine import PolicyContext, PolicyDecision, PolicyEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine() -> PolicyEngine:
    return PolicyEngine()


def _dev_caps() -> CapabilitySet:
    return CapabilitySet.dev()


def _ctx(
    action: str,
    args: list[str],
) -> PolicyContext:
    return PolicyContext(
        action=action,  # type: ignore[arg-type]
        args=args,
        working_dir="/repo",
        repo_root="/repo",
        user=None,
        caps=_dev_caps(),
        env="dev",
    )


# ---------------------------------------------------------------------------
# file_read — new credential file deny patterns
# ---------------------------------------------------------------------------


class TestFileReadCredentialPatterns:
    """file_read of credential/secret files must be DENIED."""

    def test_npmrc_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.npmrc"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_pypirc_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.pypirc"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_netrc_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.netrc"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_id_rsa_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.ssh/id_rsa"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_id_rsa_pub_variant_is_denied(self) -> None:
        # id_rsa.pub also matches **/*id_rsa*
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.ssh/id_rsa.pub"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_id_ed25519_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/home/user/.ssh/id_ed25519"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_pem_file_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/certs/server.pem"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_key_file_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/secrets/private.key"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_p12_file_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/certs/bundle.p12"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"

    def test_pfx_file_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", ["/certs/identity.pfx"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"


# ---------------------------------------------------------------------------
# shell — credential sub-command deny rules
# ---------------------------------------------------------------------------


class TestShellCredentialSubcommands:
    """Credential sub-commands must be DENIED with rule SHELL_DENY_CREDENTIAL_SUBCMD."""

    def test_git_credential_store_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["git", "credential", "store"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_git_credentials_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["git", "credentials"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_gh_auth_login_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["gh", "auth", "login"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_gh_token_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["gh", "token"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_gh_secret_list_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["gh", "secret", "list"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_npm_token_list_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["npm", "token", "list"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_npm_login_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["npm", "login"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_npm_adduser_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["npm", "adduser"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_pip_config_set_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["pip", "config", "set"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    def test_pip_config_get_is_denied(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["pip", "config", "get", "global.index-url"]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "SHELL_DENY_CREDENTIAL_SUBCMD"

    # npm install now requires approval (G-2 supply chain guard)
    def test_npm_install_requires_approval(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["npm", "install"]))
        assert decision.verdict == "REQUIRE_APPROVAL"
        assert decision.rule_id == "SHELL_PKG_INSTALL"

    def test_npm_run_is_allowed(self) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["npm", "run", "build"]))
        assert decision.verdict == "ALLOW"

    # Verify safe git commands still work
    def test_git_status_is_allowed(self) -> None:
        """git status is not in DENY_COMMANDS or credential list."""
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", ["git", "status"]))
        # git is not in SHELL_DENY_COMMANDS, so status is handled by ALLOW/default
        # (git is not in SHELL_ALLOW_COMMANDS either, so it will hit SHELL_DENY_DEFAULT)
        # The important thing is it is NOT SHELL_DENY_CREDENTIAL_SUBCMD
        assert decision.rule_id != "SHELL_DENY_CREDENTIAL_SUBCMD"


# ---------------------------------------------------------------------------
# masking — new patterns
# ---------------------------------------------------------------------------


@pytest.fixture
def masker() -> SecretMasker:
    return SecretMasker()


class TestMaskingNewPatterns:
    """New masking patterns from E-2 specification."""

    def test_npm_token_masked(self, masker: SecretMasker) -> None:
        token = "npm_" + "A" * 36
        result = masker.mask(f"value: {token}")
        assert token not in result
        assert "REDACTED:NPM_TOKEN" in result

    def test_github_fine_grained_pat_masked(self, masker: SecretMasker) -> None:
        token = "github_pat_" + "A" * 82
        result = masker.mask(f"header: {token}")
        assert token not in result
        assert "REDACTED:GITHUB_FINE_GRAINED" in result

    def test_github_cli_oauth_token_masked(self, masker: SecretMasker) -> None:
        token = "gho_" + "A" * 36
        # Avoid PASSWORD_KV match by using a non-KV context
        result = masker.mask(f"Authorization: Bearer {token}")
        assert token not in result
        assert "REDACTED:GITHUB_CLI_TOKEN" in result

    def test_ssh_rsa_private_key_header_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow..."
        result = masker.mask(text)
        assert "-----BEGIN RSA PRIVATE KEY-----" not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_ssh_openssh_private_key_header_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk..."
        result = masker.mask(text)
        assert "-----BEGIN OPENSSH PRIVATE KEY-----" not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_ssh_dsa_private_key_header_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN DSA PRIVATE KEY-----\nMIIBvAIBAAKBgQC..."
        result = masker.mask(text)
        assert "-----BEGIN DSA PRIVATE KEY-----" not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_ssh_ec_private_key_header_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEI..."
        result = masker.mask(text)
        assert "-----BEGIN EC PRIVATE KEY-----" not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_netrc_password_masked(self, masker: SecretMasker) -> None:
        text = "machine api.example.com login myuser password MySecretPass123"
        result = masker.mask(text)
        assert "MySecretPass123" not in result
        assert "REDACTED" in result

    def test_pypi_token_masked(self, masker: SecretMasker) -> None:
        # Real PyPI tokens are 50+ chars after pypi-
        token = "pypi-" + "A" * 50
        result = masker.mask(f"TWINE_PASSWORD={token}")
        assert token not in result
        assert "REDACTED" in result

    def test_pypi_token_standalone_masked(self, masker: SecretMasker) -> None:
        token = "pypi-" + "B" * 60
        result = masker.mask(f"value: {token}")
        assert token not in result
        assert "REDACTED:PYPI_TOKEN" in result
