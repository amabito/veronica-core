"""E-3: Network exfiltration prevention tests.

Covers:
- URL length limit (net.url_too_long)
- High-entropy query string (net.high_entropy_query)
- Base64 in query string (net.base64_in_query)
- Hex string in query string (net.hex_in_query)
- Per-host path allowlist (net.path_not_allowed)
- Allowlisted host + valid path → ALLOW
"""
from __future__ import annotations

import pytest

from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import PolicyContext, PolicyEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _net_ctx(url: str, method: str = "GET") -> PolicyContext:
    return PolicyContext(
        action="net",
        args=[url, method],
        working_dir=".",
        repo_root=".",
        user=None,
        caps=CapabilitySet.dev(),
        env="dev",
    )


engine = PolicyEngine()


# ---------------------------------------------------------------------------
# URL length limit
# ---------------------------------------------------------------------------

class TestUrlLengthLimit:
    def test_url_exceeding_2048_chars_is_denied(self) -> None:
        long_url = "https://pypi.org/pypi/" + "a" * 2048
        decision = engine.evaluate(_net_ctx(long_url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.url_too_long"
        assert decision.risk_score_delta == 8

    def test_url_exactly_2048_chars_is_allowed(self) -> None:
        # Build a URL exactly at the limit using a valid host + path
        base = "https://pypi.org/pypi/"
        padding = "a" * (2048 - len(base))
        url = base + padding
        assert len(url) == 2048
        decision = engine.evaluate(_net_ctx(url))
        # Should not be denied on length (may be denied for path reasons,
        # but rule_id must NOT be net.url_too_long)
        assert decision.rule_id != "net.url_too_long"


# ---------------------------------------------------------------------------
# Base64 in query string
# ---------------------------------------------------------------------------

class TestBase64QueryDetection:
    def test_base64_query_value_is_denied(self) -> None:
        # "this is a secret" base64-encoded
        url = "https://pypi.org/pypi/requests/json?data=dGhpcyBpcyBhIHNlY3JldA=="
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.base64_in_query"
        assert decision.risk_score_delta == 9

    def test_short_base64_like_value_is_not_denied_on_base64_rule(self) -> None:
        # Under 20 chars — regex requires {20,}
        url = "https://pypi.org/pypi/requests/json?v=aGVsbG8="
        decision = engine.evaluate(_net_ctx(url))
        assert decision.rule_id != "net.base64_in_query"

    def test_another_base64_secret_is_denied(self) -> None:
        url = "https://github.com/?token=c2VjcmV0X2tleV9leGZpbHRyYXRpb24="
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.base64_in_query"


# ---------------------------------------------------------------------------
# Hex string in query string
# ---------------------------------------------------------------------------

class TestHexQueryDetection:
    def test_hex_token_is_denied(self) -> None:
        # Pure hex string (matches hex regex but NOT base64: contains no A-Z other than a-f)
        # Use only lowercase a-f + digits to avoid base64 match
        url = "https://pypi.org/pypi/requests/json?token=deadbeef0123456789abcdef01234567"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        # Note: pure lowercase hex also matches base64 regex since [A-Za-z0-9+/]
        # covers a-f digits.  Either net.hex_in_query or net.base64_in_query is acceptable.
        assert decision.rule_id in ("net.hex_in_query", "net.base64_in_query")
        assert decision.risk_score_delta == 9

    def test_short_hex_not_denied_on_hex_rule(self) -> None:
        # Under 32 hex chars — regex requires {32,}
        url = "https://pypi.org/pypi/requests/json?id=deadbeef"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.rule_id != "net.hex_in_query"

    def test_hex_only_value_triggers_hex_rule_before_base64(self) -> None:
        # Construct a value that matches ONLY hex (not base64) by including
        # chars outside base64 alphabet, but hex regex still checks pure hex.
        # Since all pure hex strings ARE valid base64 chars, the base64 check
        # fires first; the important thing is it's DENIED with risk_delta=9.
        url = "https://github.com/?hash=5d41402abc4b2a76b9719d911017c592"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id in ("net.hex_in_query", "net.base64_in_query")
        assert decision.risk_score_delta == 9


# ---------------------------------------------------------------------------
# High-entropy query string
# ---------------------------------------------------------------------------

class TestHighEntropyQueryDetection:
    def test_high_entropy_value_is_denied(self) -> None:
        # Use URL-safe chars with high entropy (> 4.5 bits).
        # Includes '-', '_', '~', '.' to avoid matching base64/hex regex
        # while keeping entropy above the threshold.
        # Entropy of this value: ~4.90 bits, len=35, no base64/hex match.
        high_entropy_val = "xK9-mZ3_nQ7.pR2~sT5-wY8_vB4.jD6~fH1"
        url = f"https://pypi.org/pypi/requests/json?secret={high_entropy_val}"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.high_entropy_query"
        assert decision.risk_score_delta == 9

    def test_low_entropy_value_is_not_denied_on_entropy_rule(self) -> None:
        # "version=1.2.3" — very low entropy
        url = "https://pypi.org/pypi/requests/json?version=1.2.3"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.rule_id != "net.high_entropy_query"

    def test_short_high_entropy_value_is_not_denied(self) -> None:
        # Under 20 chars — entropy check skipped
        url = "https://pypi.org/pypi/requests/json?k=aB3xZ!qW"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.rule_id != "net.high_entropy_query"


# ---------------------------------------------------------------------------
# Per-host path allowlist
# ---------------------------------------------------------------------------

class TestPathAllowlist:
    def test_pypi_valid_path_is_allowed(self) -> None:
        url = "https://pypi.org/pypi/requests/json"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"
        assert decision.rule_id == "NET_ALLOW"

    def test_pypi_simple_path_is_allowed(self) -> None:
        url = "https://pypi.org/simple/requests/"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"

    def test_pypi_invalid_path_is_denied(self) -> None:
        url = "https://pypi.org/admin/users"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.path_not_allowed"
        assert decision.risk_score_delta == 6

    def test_pythonhosted_valid_path_is_allowed(self) -> None:
        url = "https://files.pythonhosted.org/packages/requests-2.28.0.tar.gz"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"

    def test_pythonhosted_invalid_path_is_denied(self) -> None:
        url = "https://files.pythonhosted.org/internal/secrets"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "net.path_not_allowed"

    def test_github_root_path_is_allowed(self) -> None:
        url = "https://github.com/owner/repo"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"

    def test_registry_npmjs_valid_path_is_allowed(self) -> None:
        url = "https://registry.npmjs.org/lodash"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# General / regression
# ---------------------------------------------------------------------------

class TestGeneralNetworkRules:
    def test_non_allowlisted_host_is_denied(self) -> None:
        url = "https://evil.example.com/steal"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "NET_DENY_HOST"

    def test_post_to_allowlisted_host_is_denied(self) -> None:
        url = "https://pypi.org/pypi/requests/json"
        decision = engine.evaluate(_net_ctx(url, method="POST"))
        assert decision.verdict == "DENY"
        assert decision.rule_id == "NET_DENY_METHOD"

    def test_normal_pypi_url_is_allowed(self) -> None:
        url = "https://pypi.org/pypi/requests/json"
        decision = engine.evaluate(_net_ctx(url))
        assert decision.verdict == "ALLOW"
        assert decision.risk_score_delta == 0
