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
from veronica_core.security.policy_engine import PolicyContext, PolicyEngine


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

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/home/user/.npmrc", id="npmrc"),
            pytest.param("/home/user/.pypirc", id="pypirc"),
            pytest.param("/home/user/.netrc", id="netrc"),
            pytest.param("/home/user/.ssh/id_rsa", id="id_rsa"),
            # id_rsa.pub also matches **/*id_rsa*
            pytest.param("/home/user/.ssh/id_rsa.pub", id="id_rsa_pub_variant"),
            pytest.param("/home/user/.ssh/id_ed25519", id="id_ed25519"),
            pytest.param("/certs/server.pem", id="pem_file"),
            pytest.param("/secrets/private.key", id="key_file"),
            pytest.param("/certs/bundle.p12", id="p12_file"),
            pytest.param("/certs/identity.pfx", id="pfx_file"),
        ],
    )
    def test_sensitive_file_is_denied(self, path: str) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("file_read", [path]))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "FILE_READ_DENY_SENSITIVE"


# ---------------------------------------------------------------------------
# shell — credential sub-command deny rules
# ---------------------------------------------------------------------------


class TestShellCredentialSubcommands:
    """Credential sub-commands must be DENIED with rule SHELL_DENY_CREDENTIAL_SUBCMD."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["git", "credential", "store"], id="git_credential_store"),
            pytest.param(["git", "credentials"], id="git_credentials"),
            pytest.param(["gh", "auth", "login"], id="gh_auth_login"),
            pytest.param(["gh", "token"], id="gh_token"),
            pytest.param(["gh", "secret", "list"], id="gh_secret_list"),
            pytest.param(["npm", "token", "list"], id="npm_token_list"),
            pytest.param(["npm", "login"], id="npm_login"),
            pytest.param(["npm", "adduser"], id="npm_adduser"),
            pytest.param(["pip", "config", "set"], id="pip_config_set"),
            pytest.param(["pip", "config", "get", "global.index-url"], id="pip_config_get"),
        ],
    )
    def test_credential_subcmd_is_denied(self, args: list[str]) -> None:
        engine = _engine()
        decision = engine.evaluate(_ctx("shell", args))
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

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param("-----BEGIN OPENSSH PRIVATE KEY-----", id="openssh"),
            pytest.param("-----BEGIN DSA PRIVATE KEY-----", id="dsa"),
        ],
    )
    def test_ssh_private_key_header_masked(self, masker: SecretMasker, header: str) -> None:
        text = f"{header}\nMIIBvAIBAAKBgQC..."
        result = masker.mask(text)
        assert header not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_netrc_password_masked(self, masker: SecretMasker) -> None:
        text = "machine api.example.com login myuser password MySecretPass123"
        result = masker.mask(text)
        assert "MySecretPass123" not in result
        assert "REDACTED" in result

    def test_pypi_token_standalone_masked(self, masker: SecretMasker) -> None:
        token = "pypi-" + "B" * 60
        result = masker.mask(f"value: {token}")
        assert token not in result
        assert "REDACTED:PYPI_TOKEN" in result
