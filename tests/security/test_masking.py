"""Tests for SecretMasker — expanded secret classification (Phase E-2)."""
from __future__ import annotations

import pytest

from veronica_core.security.masking import SecretMasker


@pytest.fixture
def masker() -> SecretMasker:
    return SecretMasker()


# ---------------------------------------------------------------------------
# Existing patterns (regression)
# ---------------------------------------------------------------------------


class TestExistingPatterns:
    """Regression coverage for patterns that existed before E-2."""

    def test_aws_access_key_is_masked(self, masker: SecretMasker) -> None:
        result = masker.mask("key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "REDACTED:AWS_KEY" in result

    def test_github_classic_pat_is_masked(self, masker: SecretMasker) -> None:
        # Pass token as standalone (not in key=value form) to avoid PASSWORD_KV match
        token = "ghp_" + "A" * 36
        result = masker.mask(f"Authorization: Bearer {token}")
        assert token not in result
        assert "REDACTED:GITHUB_TOKEN" in result

    def test_stripe_live_secret_is_masked(self, masker: SecretMasker) -> None:
        key = "sk_live_" + "x" * 24
        result = masker.mask(f"value is {key} here")
        assert key not in result
        assert "REDACTED:STRIPE_KEY" in result

    def test_password_kv_is_masked(self, masker: SecretMasker) -> None:
        result = masker.mask("password=hunter2secret")
        assert "hunter2secret" not in result
        assert "REDACTED:PASSWORD_KV" in result

    def test_hex_secret_is_masked(self, masker: SecretMasker) -> None:
        hex_val = "a" * 32
        result = masker.mask(f"key={hex_val}")
        assert "REDACTED" in result


# ---------------------------------------------------------------------------
# New patterns (E-2 expansion)
# ---------------------------------------------------------------------------


class TestOpenAIKeyMasking:
    def test_legacy_sk_key_is_masked(self, masker: SecretMasker) -> None:
        # Pass as bare value to confirm OPENAI_KEY pattern matches
        key = "sk-" + "T" * 48
        result = masker.mask(f"Authorization: Bearer {key}")
        assert key not in result
        assert "REDACTED:OPENAI_KEY" in result

    def test_project_scoped_key_is_masked(self, masker: SecretMasker) -> None:
        key = "sk-proj-" + "T" * 48
        result = masker.mask(f"value is {key} here")
        assert key not in result
        assert "REDACTED:OPENAI_KEY" in result


class TestAnthropicKeyMasking:
    def test_anthropic_key_is_masked(self, masker: SecretMasker) -> None:
        key = "sk-ant-api03-" + "A" * 30
        # Use a context that doesn't trigger PASSWORD_KV (avoid token/secret/key= forms)
        result = masker.mask(f"Bearer {key} in header")
        assert key not in result
        assert "REDACTED:ANTHROPIC_KEY" in result


class TestSlackTokenMasking:
    def test_slack_bot_token_is_masked(self, masker: SecretMasker) -> None:
        token = "xoxb-123456-654321-" + "a" * 24
        result = masker.mask(f"Authorization: Bearer {token}")
        assert token not in result
        assert "REDACTED:SLACK_TOKEN" in result

    def test_slack_user_token_is_masked(self, masker: SecretMasker) -> None:
        token = "xoxp-" + "9" * 30
        result = masker.mask(f"value is {token}")
        assert token not in result
        assert "REDACTED:SLACK_TOKEN" in result

    def test_slack_webhook_url_is_masked(self, masker: SecretMasker) -> None:
        url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXX"
        result = masker.mask(f"webhook URL: {url}")
        assert url not in result
        assert "REDACTED:SLACK_WEBHOOK" in result


class TestDiscordTokenMasking:
    def test_discord_bot_token_is_masked(self, masker: SecretMasker) -> None:
        # Discord bot tokens: MFA.xxxx.xxxx (24 + 6 + 27 chars)
        token = "MTIzNDU2Nzg5MDEyMzQ1Njc4" + "." + "A" * 6 + "." + "B" * 27
        # Avoid PASSWORD_KV: use "Discord bot:" prefix without token=/secret= form
        result = masker.mask(f"Discord bot: {token}")
        assert token not in result
        assert "REDACTED:DISCORD_TOKEN" in result


class TestTwilioMasking:
    def test_twilio_account_sid_is_masked(self, masker: SecretMasker) -> None:
        sid = "AC" + "a" * 32
        result = masker.mask(f"sid value: {sid}")
        assert sid not in result
        assert "REDACTED:TWILIO_SID" in result

    def test_twilio_auth_token_is_masked(self, masker: SecretMasker) -> None:
        # TWILIO_TOKEN pattern matches "twilio...token=value" — value gets masked
        result = masker.mask("twilio_token=secret_auth_token_value_here")
        assert "secret_auth_token_value_here" not in result
        # Either TWILIO_TOKEN or PASSWORD_KV will catch it — either is correct
        assert "REDACTED" in result


class TestSendGridKeyMasking:
    def test_sendgrid_key_is_masked(self, masker: SecretMasker) -> None:
        key = "SG." + "A" * 22 + "." + "B" * 43
        result = masker.mask(f"sendgrid key: {key}")
        assert key not in result
        assert "REDACTED:SENDGRID_KEY" in result


class TestGoogleKeyMasking:
    def test_google_api_key_is_masked(self, masker: SecretMasker) -> None:
        key = "AIza" + "T" * 35
        result = masker.mask(f"google key: {key}")
        assert key not in result
        assert "REDACTED:GOOGLE_API_KEY" in result

    def test_google_oauth_client_secret_is_masked(self, masker: SecretMasker) -> None:
        secret = "GOCSPX-" + "A" * 28
        # Avoid PASSWORD_KV false match by using a context without secret=/token= form
        result = masker.mask(f"client value: {secret}")
        assert secret not in result
        assert "REDACTED:GOOGLE_OAUTH" in result


class TestPrivateKeyBlockMasking:
    def test_rsa_private_key_header_is_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow..."
        result = masker.mask(text)
        assert "-----BEGIN RSA PRIVATE KEY-----" not in result
        # Label renamed from PRIVATE_KEY_BLOCK to SSH_PRIVATE_KEY in E-2
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_ec_private_key_header_is_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN EC PRIVATE KEY-----\nMHQCAQ..."
        result = masker.mask(text)
        assert "-----BEGIN EC PRIVATE KEY-----" not in result
        assert "REDACTED:SSH_PRIVATE_KEY" in result

    def test_pgp_private_key_header_is_masked(self, masker: SecretMasker) -> None:
        text = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQHYBG..."
        result = masker.mask(text)
        assert "-----BEGIN PGP PRIVATE KEY BLOCK-----" not in result
        assert "REDACTED:PGP_PRIVATE_KEY" in result


class TestNpmPypiTokenMasking:
    def test_npm_token_is_masked(self, masker: SecretMasker) -> None:
        token = "npm_" + "A" * 36
        # Use NODE_AUTH_TOKEN= form which is not caught by PASSWORD_KV
        # (PASSWORD_KV catches token= but not NODE_AUTH_TOKEN=)
        result = masker.mask(f"value: {token}")
        assert token not in result
        assert "REDACTED:NPM_TOKEN" in result

    def test_pypi_token_is_masked(self, masker: SecretMasker) -> None:
        # PyPI tokens are 50+ chars after pypi- (updated threshold in E-2)
        token = "pypi-" + "A" * 50
        result = masker.mask(f"value: {token}")
        assert token not in result
        assert "REDACTED:PYPI_TOKEN" in result


class TestPolymarketKeyMasking:
    def test_polymarket_api_key_is_masked(self, masker: SecretMasker) -> None:
        result = masker.mask("polymarket_api_key=some_secret_value_32chars_x")
        assert "some_secret_value_32chars_x" not in result
        assert "REDACTED" in result


# ---------------------------------------------------------------------------
# Aggregate / integration tests
# ---------------------------------------------------------------------------


class TestMaskDict:
    def test_mask_dict_masks_nested_values(self, masker: SecretMasker) -> None:
        key = "sk-" + "T" * 48
        d = {"credentials": {"openai_key": key, "count": 5}}
        result = masker.mask_dict(d)
        assert key not in str(result)

    def test_mask_dict_preserves_non_secret_values(self, masker: SecretMasker) -> None:
        d = {"name": "Alice", "age": 30}
        result = masker.mask_dict(d)
        assert result == {"name": "Alice", "age": 30}

    def test_mask_args_masks_list_elements(self, masker: SecretMasker) -> None:
        token = "xoxb-123456-654321-" + "a" * 24
        args = ["--token", token, "--verbose"]
        result = masker.mask_args(args)
        assert token not in " ".join(result)


class TestNoFalsePositives:
    """Ensure common safe strings are not incorrectly masked."""

    def test_short_hex_git_sha_not_masked(self, masker: SecretMasker) -> None:
        # 7-char short SHA is below HEX_SECRET threshold (32 chars)
        result = masker.mask("commit abc1234 is fine")
        assert "REDACTED" not in result

    def test_normal_url_not_masked(self, masker: SecretMasker) -> None:
        result = masker.mask("Visit https://example.com for info")
        assert "REDACTED" not in result

    def test_plain_english_word_not_masked(self, masker: SecretMasker) -> None:
        result = masker.mask("The quick brown fox")
        assert "REDACTED" not in result
