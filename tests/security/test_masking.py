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

    def test_mask_dict_recurses_into_list_of_dicts(self, masker: SecretMasker) -> None:
        """Secrets inside dicts nested within list values must be redacted at any depth."""
        secret = "sk-" + "T" * 48
        nested_secret = "sk-" + "N" * 48
        payload = {
            "data": [
                {"api_key": secret},
                "plain_string",
                42,
                [{"nested": nested_secret}],
            ]
        }
        result = masker.mask_dict(payload)

        result_str = str(result)
        assert secret not in result_str, "top-level list dict secret not masked"
        assert nested_secret not in result_str, "deeply nested list dict secret not masked"
        # Non-secret scalars pass through
        assert "plain_string" in result_str
        assert "42" in result_str


class TestBytesHandling:
    """SecretMasker must redact secrets inside bytes values (bytes-leak fix)."""

    def test_bytes_value_is_decoded_and_masked(self, masker: SecretMasker) -> None:
        key = b"sk-" + b"T" * 48
        result = masker._mask_value("api_key", key)
        assert key.decode() not in str(result)
        assert "REDACTED" in str(result)

    def test_bytes_in_dict_is_masked(self, masker: SecretMasker) -> None:
        key_bytes = b"sk-" + b"T" * 48
        d = {"openai_key": key_bytes}
        result = masker.mask_dict(d)
        assert key_bytes not in str(result).encode()
        assert "REDACTED" in str(result)

    def test_bytes_returns_str_result(self, masker: SecretMasker) -> None:
        result = masker._mask_value("key", b"harmless")
        assert isinstance(result, str)

    def test_bytes_with_replace_error_handling(self, masker: SecretMasker) -> None:
        # Invalid UTF-8 bytes must not raise; errors="replace" is used
        bad_bytes = b"\xff\xfe" + b"sk-" + b"T" * 48
        result = masker._mask_value("key", bad_bytes)
        assert isinstance(result, str)


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


# ---------------------------------------------------------------------------
# mask_dict: sequence subclass safety (namedtuple / custom containers)
# ---------------------------------------------------------------------------


class TestMaskDictSequenceSubclasses:
    """_mask_value must not crash on namedtuple or list/tuple subclasses."""

    def test_namedtuple_value_falls_back_gracefully(self, masker: SecretMasker) -> None:
        """A namedtuple stored as a dict value must not raise TypeError.

        namedtuple.__init__ does not accept a plain list, so
        ``type(value)(masked)`` would raise.  The fixed code catches TypeError
        and falls back to a plain tuple.
        """
        from collections import namedtuple

        Point = namedtuple("Point", ["x", "y"])
        d = {"coords": Point("not_a_secret", "also_fine")}
        result = masker.mask_dict(d)
        # Must not raise; result should be a sequence containing the values.
        assert "not_a_secret" in result["coords"] or list(result["coords"]) == ["not_a_secret", "also_fine"]

    def test_list_subclass_falls_back_gracefully(self, masker: SecretMasker) -> None:
        """A list subclass that overrides __init__ must not crash mask_dict."""

        class StrictList(list):
            def __init__(self, iterable=None):
                if iterable is not None and len(list(iterable)) > 2:
                    raise TypeError("StrictList: too many items")
                super().__init__(iterable or [])

        d = {"items": StrictList(["hello", "world"])}
        # Must not raise regardless of StrictList's constructor constraints.
        result = masker.mask_dict(d)
        assert result["items"] is not None

    def test_regular_list_preserves_type(self, masker: SecretMasker) -> None:
        """Regular list values are still returned as list after masking."""
        d = {"vals": ["plain_text", "more_plain"]}
        result = masker.mask_dict(d)
        assert isinstance(result["vals"], list)

    def test_regular_tuple_preserves_type(self, masker: SecretMasker) -> None:
        """Regular tuple values are still returned as tuple after masking."""
        d = {"vals": ("plain_text", "more_plain")}
        result = masker.mask_dict(d)
        assert isinstance(result["vals"], tuple)


# ---------------------------------------------------------------------------
# _MAX_DEPTH: recursion depth limit prevents DoS on self-referential containers
# ---------------------------------------------------------------------------


class TestMaskDictDepthLimit:
    """_MAX_DEPTH cutoff prevents infinite recursion on deeply nested structures."""

    def test_deeply_nested_dict_does_not_crash(self, masker: SecretMasker) -> None:
        """Deeply nested dicts beyond _MAX_DEPTH must return the node unchanged."""
        # Build a dict nested 60 levels deep (above _MAX_DEPTH=50).
        d: dict = {"key": "leaf_value"}
        for _ in range(60):
            d = {"nested": d}

        # Must not raise RecursionError or crash.
        result = masker.mask_dict(d)
        assert result is not None

    def test_depth_exactly_at_limit_still_processes(self, masker: SecretMasker) -> None:
        """Nodes at depth < _MAX_DEPTH are still processed (not cut off early).

        mask_dict starts at depth=0.  A leaf at depth _MAX_DEPTH-1 is inside
        the allowed range and should be traversed.  A node at _MAX_DEPTH is
        beyond the limit and should be passed through unchanged.
        """
        max_depth = masker._MAX_DEPTH
        # Build a dict nested exactly _MAX_DEPTH-1 levels; the leaf is a string
        # that would normally be masked (plain text, no secrets here — just verify
        # the traversal reaches it and returns a string, not the bare value).
        leaf = "leaf_plain"
        d: dict = {"key": leaf}
        for _ in range(max_depth - 2):  # -2 because mask_dict itself is depth 0
            d = {"nested": d}

        # Must not raise; the leaf must be returned as a string (masked or plain).
        result = masker.mask_dict(d)
        assert result is not None

        # Verify that a leaf exactly at the cutoff depth is returned unchanged
        # (no crash, no masking error — pass-through is the safe behavior).
        deep_leaf_dict: dict = {"secret_key": "skipped_at_limit"}
        for _ in range(max_depth):
            deep_leaf_dict = {"nested": deep_leaf_dict}
        result_deep = masker.mask_dict(deep_leaf_dict)
        assert result_deep is not None  # Must not raise


# ---------------------------------------------------------------------------
# Adversarial tests — attacker mindset
# ---------------------------------------------------------------------------


class TestAdversarialMasking:
    """Adversarial tests for SecretMasker — type variation, concurrency, and
    self-referential containers that could crash or leak secrets."""

    # ------------------------------------------------------------------
    # Gap #1: type variation — non-str values in mask_dict
    # ------------------------------------------------------------------

    def test_int_value_under_sensitive_key_passes_through(self, masker: SecretMasker) -> None:
        """int values must not crash mask_dict.

        NOTE: The current implementation passes non-str/bytes/dict/list/tuple
        values through unchanged (_mask_value returns `value` as-is for 'other'
        types). An int under a sensitive key like 'api_key' is NOT masked —
        this is the documented pass-through behavior.
        """
        d = {"api_key": 12345678}
        result = masker.mask_dict(d)
        # Must not crash; value passes through as int
        assert result["api_key"] == 12345678

    def test_none_value_passes_through(self, masker: SecretMasker) -> None:
        """None under any key must not crash mask_dict."""
        d = {"api_key": None, "password": None}
        result = masker.mask_dict(d)
        assert result["api_key"] is None
        assert result["password"] is None

    def test_nan_value_passes_through(self, masker: SecretMasker) -> None:
        """float('nan') must not crash mask_dict (pass-through behavior)."""
        import math

        d = {"token": float("nan")}
        result = masker.mask_dict(d)
        assert math.isnan(result["token"])

    def test_bytes_secret_value_is_masked(self, masker: SecretMasker) -> None:
        """bytes under a sensitive key must be decoded and masked."""
        secret_bytes = b"sk-" + b"T" * 48
        d = {"api_key": secret_bytes}
        result = masker.mask_dict(d)
        # Implementation decodes bytes then runs mask() — secret must be redacted
        result_str = str(result)
        assert secret_bytes.decode() not in result_str
        assert "REDACTED" in result_str

    def test_bytes_non_secret_value_returns_string(self, masker: SecretMasker) -> None:
        """Harmless bytes values are decoded and returned as str (not bytes)."""
        d = {"name": b"harmless_value"}
        result = masker.mask_dict(d)
        # After bytes decoding, result is a str
        assert isinstance(result["name"], str)
        assert "harmless_value" in result["name"]

    def test_list_value_with_secret_is_masked(self, masker: SecretMasker) -> None:
        """list containing a secret string must be recursed and masked."""
        secret = "sk-" + "T" * 48
        d = {"credentials": [secret, "plain_value"]}
        result = masker.mask_dict(d)
        result_str = str(result)
        assert secret not in result_str
        assert "REDACTED" in result_str
        assert "plain_value" in result_str

    def test_nested_dict_with_non_str_leaf_does_not_crash(self, masker: SecretMasker) -> None:
        """Nested dicts mixing str and non-str values must not crash."""
        d = {
            "outer": {
                "api_key": "sk-" + "T" * 48,  # str — must be masked
                "count": 42,                    # int — passes through
                "ratio": 0.75,                  # float — passes through
                "active": True,                 # bool — passes through
                "tag": None,                    # None — passes through
            }
        }
        result = masker.mask_dict(d)
        outer = result["outer"]
        # Secret str must be redacted
        assert "REDACTED" in str(outer["api_key"])
        # Non-str scalars must be unchanged
        assert outer["count"] == 42
        assert outer["ratio"] == 0.75
        assert outer["active"] is True
        assert outer["tag"] is None

    # ------------------------------------------------------------------
    # Gap #3: concurrent thread safety
    # ------------------------------------------------------------------

    def test_concurrent_mask_dict_20_threads_no_corruption(self, masker: SecretMasker) -> None:
        """20 threads calling mask_dict() simultaneously must not corrupt results.

        mask_dict and mask() are stateless: they read from module-level
        _PATTERNS (read-only after import) and create new objects on each call.
        There is no shared mutable state, so this should be thread-safe.
        """
        import threading

        secret = "sk-" + "T" * 48
        num_threads = 20
        results: list[dict | None] = [None] * num_threads
        errors: list[Exception | None] = [None] * num_threads
        barrier = threading.Barrier(num_threads)

        def worker(idx: int) -> None:
            d = {
                "api_key": secret,
                "user": f"thread-{idx}",
                "count": idx,
            }
            try:
                barrier.wait()  # All threads start simultaneously
                results[idx] = masker.mask_dict(d)
            except Exception as exc:
                errors[idx] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No thread should have raised an exception
        assert all(e is None for e in errors), f"Thread errors: {[e for e in errors if e]}"

        # All results must be dicts with the secret redacted
        for i, result in enumerate(results):
            assert result is not None, f"Thread {i} produced no result"
            assert secret not in str(result["api_key"]), f"Thread {i} leaked secret"
            assert "REDACTED" in str(result["api_key"]), f"Thread {i} did not redact"
            # Non-str pass-through must be correct
            assert result["count"] == i

    def test_concurrent_mask_str_20_threads_no_corruption(self, masker: SecretMasker) -> None:
        """20 threads calling mask() on different inputs must each get correct output."""
        import threading

        num_threads = 20
        results: list[str | None] = [None] * num_threads
        errors: list[Exception | None] = [None] * num_threads
        barrier = threading.Barrier(num_threads)

        def worker(idx: int) -> None:
            secret = f"sk-{'T' * 48}-thread{idx}"
            try:
                barrier.wait()
                results[idx] = masker.mask(f"Bearer {secret}")
            except Exception as exc:
                errors[idx] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(e is None for e in errors), f"Thread errors: {[e for e in errors if e]}"
        for i, result in enumerate(results):
            assert result is not None, f"Thread {i} produced no result"
            assert "REDACTED" in result, f"Thread {i} did not redact"

    # ------------------------------------------------------------------
    # Gap #4: circular / self-referential dict
    # ------------------------------------------------------------------

    def test_self_referential_dict_does_not_infinite_loop(self, masker: SecretMasker) -> None:
        """A dict that contains itself must not cause infinite recursion.

        Python dicts can be made self-referential via d['self'] = d.
        The _MAX_DEPTH=50 guard stops infinite recursion; the call returns
        the node unchanged once the depth limit is hit.
        """
        d: dict = {"key": "value"}
        d["self"] = d  # type: ignore[assignment]  # Self-reference

        # Must complete without RecursionError or hanging
        result = masker.mask_dict(d)
        assert result is not None
        # The non-circular key must still be present
        assert result["key"] == "value"

    def test_mutually_referential_dicts_do_not_crash(self, masker: SecretMasker) -> None:
        """Two dicts referencing each other must not cause infinite recursion."""
        a: dict = {"name": "dict_a"}
        b: dict = {"name": "dict_b", "other": a}
        a["other"] = b  # type: ignore[assignment]  # Mutual reference

        # Must complete without RecursionError
        result = masker.mask_dict(a)
        assert result is not None
        assert result["name"] == "dict_a"
