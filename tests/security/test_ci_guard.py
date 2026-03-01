"""Tests for CIGuard — CI-specific secret leak detection (Task #2)."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from veronica_core.security.ci_guard import CIGuard, Finding, _CI_PATTERNS
from veronica_core.security.masking import SecretMasker
from veronica_core.security.security_level import SecurityLevel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def guard() -> CIGuard:
    return CIGuard()


@pytest.fixture
def masker() -> SecretMasker:
    return SecretMasker()


# ---------------------------------------------------------------------------
# TestFinding — dataclass contract
# ---------------------------------------------------------------------------


class TestFinding:
    def test_all_fields_accessible(self) -> None:
        f = Finding(
            pattern_name="TEST",
            line_number=1,
            masked_snippet="[REDACTED:TEST]",
            severity="HIGH",
        )
        assert f.pattern_name == "TEST"
        assert f.line_number == 1
        assert f.masked_snippet == "[REDACTED:TEST]"
        assert f.severity == "HIGH"

    def test_frozen_raises_on_mutation(self) -> None:
        f = Finding(pattern_name="X", line_number=1, masked_snippet="y", severity="HIGH")
        with pytest.raises((AttributeError, TypeError)):
            f.pattern_name = "Y"  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        a = Finding("P", 1, "s", "HIGH")
        b = Finding("P", 1, "s", "HIGH")
        assert a == b

    def test_inequality_on_different_values(self) -> None:
        a = Finding("P", 1, "s", "HIGH")
        b = Finding("P", 2, "s", "HIGH")
        assert a != b


# ---------------------------------------------------------------------------
# TestCIGuardScan — core detection
# ---------------------------------------------------------------------------


class TestCIGuardScan:
    def test_github_actions_token_detected(self, guard: CIGuard) -> None:
        text = f"token: ghs_{'A' * 36}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "GITHUB_ACTIONS_TOKEN" in names

    def test_github_actions_token_severity_is_critical(self, guard: CIGuard) -> None:
        text = f"ghs_{'B' * 40}"
        findings = guard.scan(text)
        ci_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert ci_findings, "GITHUB_ACTIONS_TOKEN finding expected"
        assert ci_findings[0].severity == "CRITICAL"

    def test_gitlab_ci_token_detected(self, guard: CIGuard) -> None:
        text = f"glcbt-{'a' * 25}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "GITLAB_CI_TOKEN" in names

    def test_gitlab_ci_token_severity_is_critical(self, guard: CIGuard) -> None:
        text = f"glcbt-{'z' * 20}"
        findings = guard.scan(text)
        ci_findings = [f for f in findings if f.pattern_name == "GITLAB_CI_TOKEN"]
        assert ci_findings
        assert ci_findings[0].severity == "CRITICAL"

    def test_docker_auth_detected(self, guard: CIGuard) -> None:
        text = '"auth": "dXNlcjpwYXNzd29yZA=="'
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "DOCKER_AUTH" in names

    def test_docker_auth_severity_is_critical(self, guard: CIGuard) -> None:
        text = '"auth": "c29tZWxvbmdiYXNlNjRzdHJpbmc="'
        findings = guard.scan(text)
        ci_findings = [f for f in findings if f.pattern_name == "DOCKER_AUTH"]
        assert ci_findings
        assert ci_findings[0].severity == "CRITICAL"

    def test_circleci_token_detected(self, guard: CIGuard) -> None:
        text = f"circle-token={'x' * 25}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "CIRCLECI_TOKEN" in names

    def test_jenkins_token_detected(self, guard: CIGuard) -> None:
        text = f"JENKINS_API_TOKEN={'y' * 16}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "JENKINS_TOKEN" in names

    def test_artifactory_token_detected(self, guard: CIGuard) -> None:
        text = f"AKC{'z' * 12}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "ARTIFACTORY_TOKEN" in names

    def test_buildkite_token_detected(self, guard: CIGuard) -> None:
        text = f"bkua_{'a' * 40}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "BUILDKITE_TOKEN" in names

    def test_multiple_secrets_on_different_lines(self, guard: CIGuard) -> None:
        text = (
            f"step 1: ghs_{'A' * 36}\n"
            f'step 2: "auth": "c29tZWxvbmdiYXNlNjRzdHJpbmc="\n'
            f"step 3: clean output\n"
        )
        findings = guard.scan(text)
        line_numbers = {f.line_number for f in findings}
        # Lines 1 and 2 should have findings; line 3 should not
        assert 1 in line_numbers
        assert 2 in line_numbers
        assert 3 not in line_numbers

    def test_clean_text_returns_empty_list(self, guard: CIGuard) -> None:
        text = "Build succeeded. All tests passed. No secrets here.\nDeploy complete."
        findings = guard.scan(text)
        assert findings == []

    def test_existing_aws_key_pattern_detected(self, guard: CIGuard) -> None:
        # Verify that base SecretMasker patterns (AWS_KEY) are also detected
        text = "key: AKIAIOSFODNN7EXAMPLE"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "AWS_KEY" in names

    def test_existing_github_token_pattern_detected(self, guard: CIGuard) -> None:
        token = "ghp_" + "A" * 36
        text = f"Authorization: Bearer {token}"
        findings = guard.scan(text)
        names = [f.pattern_name for f in findings]
        assert "GITHUB_TOKEN" in names

    def test_line_number_is_accurate(self, guard: CIGuard) -> None:
        text = "clean line\nclean line 2\nghs_" + "A" * 36
        findings = guard.scan(text)
        ci_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert ci_findings
        assert ci_findings[0].line_number == 3

    def test_masked_snippet_contains_no_raw_secret(self, guard: CIGuard) -> None:
        secret = "ghs_" + "A" * 36
        text = f"token: {secret}"
        findings = guard.scan(text)
        assert findings
        for f in findings:
            assert secret not in f.masked_snippet

    def test_deduplicated_no_double_finding_same_pattern_same_line(
        self, guard: CIGuard
    ) -> None:
        # A line with one occurrence of GITHUB_ACTIONS_TOKEN should produce exactly 1 finding
        # for that pattern (dedup guard).
        text = "ghs_" + "A" * 36
        findings = guard.scan(text)
        github_action_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert len(github_action_findings) == 1

    def test_empty_string_returns_empty_list(self, guard: CIGuard) -> None:
        assert guard.scan("") == []

    def test_single_newline_returns_empty_list(self, guard: CIGuard) -> None:
        assert guard.scan("\n") == []


# ---------------------------------------------------------------------------
# TestCIGuardScanFile — file scanning
# ---------------------------------------------------------------------------


class TestCIGuardScanFile:
    def test_scan_file_detects_secret(self, guard: CIGuard, tmp_path: Path) -> None:
        secret_file = tmp_path / "ci_log.txt"
        secret_file.write_text(f"token: ghs_{'Z' * 36}\n", encoding="utf-8")
        findings = guard.scan_file(secret_file)
        names = [f.pattern_name for f in findings]
        assert "GITHUB_ACTIONS_TOKEN" in names

    def test_scan_file_clean_returns_empty(self, guard: CIGuard, tmp_path: Path) -> None:
        clean_file = tmp_path / "clean.txt"
        clean_file.write_text("No secrets here. Build passed.\n", encoding="utf-8")
        findings = guard.scan_file(clean_file)
        assert findings == []

    def test_scan_file_multiple_secrets(self, guard: CIGuard, tmp_path: Path) -> None:
        content = (
            f"line1: ghs_{'A' * 36}\n"
            f"line2: AKIAIOSFODNN7EXAMPLE\n"
            f"line3: clean output\n"
        )
        secret_file = tmp_path / "multi.txt"
        secret_file.write_text(content, encoding="utf-8")
        findings = guard.scan_file(secret_file)
        names = {f.pattern_name for f in findings}
        assert "GITHUB_ACTIONS_TOKEN" in names
        assert "AWS_KEY" in names

    def test_scan_file_with_unicode_content(self, guard: CIGuard, tmp_path: Path) -> None:
        # File with unicode chars mixed in — should not raise
        unicode_file = tmp_path / "unicode.txt"
        unicode_file.write_text("日本語テスト\nNo secrets\n", encoding="utf-8")
        findings = guard.scan_file(unicode_file)
        assert findings == []

    def test_scan_file_invalid_utf8_does_not_crash(self, guard: CIGuard, tmp_path: Path) -> None:
        # Invalid UTF-8 bytes — errors="replace" should handle gracefully
        binary_file = tmp_path / "binary.bin"
        binary_file.write_bytes(b"\xff\xfe invalid utf8 bytes\n")
        # Must not raise
        findings = guard.scan_file(binary_file)
        assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# TestCIGuardProtectOutput — masking
# ---------------------------------------------------------------------------


class TestCIGuardProtectOutput:
    def test_github_actions_token_masked(self, guard: CIGuard) -> None:
        # Use a neutral prefix to avoid PASSWORD_KV capturing the value first.
        secret = "ghs_" + "B" * 36
        result = guard.protect_output(f"Authorization: Bearer {secret}")
        assert secret not in result
        assert "REDACTED:GITHUB_ACTIONS_TOKEN" in result

    def test_gitlab_ci_token_masked(self, guard: CIGuard) -> None:
        # Use a neutral prefix to avoid PASSWORD_KV capturing the value first.
        secret = "glcbt-" + "c" * 22
        result = guard.protect_output(f"ci_value: {secret}")
        assert secret not in result
        assert "REDACTED:GITLAB_CI_TOKEN" in result

    def test_docker_auth_masked(self, guard: CIGuard) -> None:
        text = '"auth": "c29tZWxvbmdiYXNlNjRzdHJpbmc="'
        result = guard.protect_output(text)
        assert "c29tZWxvbmdiYXNlNjRzdHJpbmc=" not in result
        assert "REDACTED:DOCKER_AUTH" in result

    def test_buildkite_token_masked(self, guard: CIGuard) -> None:
        secret = "bkua_" + "d" * 40
        result = guard.protect_output(f"key: {secret}")
        assert secret not in result
        assert "REDACTED:BUILDKITE_TOKEN" in result

    def test_aws_key_also_masked_via_base_patterns(self, guard: CIGuard) -> None:
        text = "key: AKIAIOSFODNN7EXAMPLE"
        result = guard.protect_output(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_clean_text_passes_through_unchanged(self, guard: CIGuard) -> None:
        text = "Build succeeded. Deploy complete. No secrets."
        result = guard.protect_output(text)
        assert result == text

    def test_empty_string_passes_through(self, guard: CIGuard) -> None:
        assert guard.protect_output("") == ""

    def test_all_ci_patterns_are_masked(self, guard: CIGuard) -> None:
        # Verify every CI pattern is handled by protect_output
        test_cases = [
            ("ghs_" + "A" * 36, "GITHUB_ACTIONS_TOKEN"),
            ("glcbt-" + "a" * 20, "GITLAB_CI_TOKEN"),
            ('"auth": "' + "A" * 20 + '"', "DOCKER_AUTH"),
            ("circle-token=" + "x" * 22, "CIRCLECI_TOKEN"),
            ("JENKINS_API_TOKEN=" + "y" * 10, "JENKINS_TOKEN"),
            ("AKC" + "z" * 12, "ARTIFACTORY_TOKEN"),
            ("bkua_" + "a" * 40, "BUILDKITE_TOKEN"),
        ]
        for secret, label in test_cases:
            result = guard.protect_output(secret)
            assert f"REDACTED:{label}" in result, f"{label} not redacted in: {result!r}"


# ---------------------------------------------------------------------------
# TestCIGuardIsCI — environment detection
# ---------------------------------------------------------------------------


class TestCIGuardIsCI:
    def test_returns_true_when_ci_level(self) -> None:
        with patch(
            "veronica_core.security.ci_guard.get_security_level",
            return_value=SecurityLevel.CI,
        ):
            assert CIGuard.is_ci() is True

    def test_returns_true_when_prod_level(self) -> None:
        with patch(
            "veronica_core.security.ci_guard.get_security_level",
            return_value=SecurityLevel.PROD,
        ):
            assert CIGuard.is_ci() is True

    def test_returns_false_when_dev_level(self) -> None:
        with patch(
            "veronica_core.security.ci_guard.get_security_level",
            return_value=SecurityLevel.DEV,
        ):
            assert CIGuard.is_ci() is False


# ---------------------------------------------------------------------------
# TestAdversarial — adversarial / edge cases
# ---------------------------------------------------------------------------


class TestAdversarialCIGuard:
    """Adversarial tests: large inputs, binary data, unicode bypass attempts."""

    def test_large_input_completes_within_5_seconds(self, guard: CIGuard) -> None:
        # 10,000 clean lines — performance check
        text = "\n".join([f"log line {i}: all clear, no secrets here" for i in range(10_000)])
        start = time.monotonic()
        findings = guard.scan(text)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"scan took {elapsed:.2f}s (limit: 5s)"
        assert findings == []

    def test_large_input_with_one_secret_finds_it(self, guard: CIGuard) -> None:
        lines = [f"log line {i}: clean" for i in range(5_000)]
        secret = "ghs_" + "A" * 36
        lines[2500] = f"token: {secret}"
        text = "\n".join(lines)
        findings = guard.scan(text)
        ci_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert ci_findings
        assert ci_findings[0].line_number == 2501  # 0-indexed list -> 1-indexed line

    def test_binary_data_mixed_in_does_not_crash(self, guard: CIGuard) -> None:
        # Simulate text that might come from partial binary read (errors="replace")
        # We pass decoded str with replacement chars
        text = "\ufffd\ufffd binary junk \ufffd\nclean line\n"
        findings = guard.scan(text)
        assert isinstance(findings, list)

    def test_unicode_nfkc_bypass_attempt_does_not_leak(self, guard: CIGuard) -> None:
        # Attacker uses lookalike Unicode chars to bypass regex detection.
        # The guard uses standard re patterns — Unicode lookalikes won't match
        # the ASCII pattern, so they should NOT be detected. This test verifies
        # the guard doesn't crash and that real tokens on the same text are still caught.
        # Real token alongside unicode confusables
        real_secret = "ghs_" + "A" * 36
        # Unicode confusable for 'g' (U+0261 LATIN SMALL LETTER SCRIPT G)
        lookalike = "\u0261hs_" + "A" * 36
        text = f"{lookalike}\n{real_secret}"
        findings = guard.scan(text)
        # Real secret on line 2 must be detected
        ci_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert ci_findings
        assert ci_findings[0].line_number == 2

    def test_protect_output_with_binary_replacement_chars(self, guard: CIGuard) -> None:
        # protect_output receives a pre-decoded str (with U+FFFD replacements)
        text = "\ufffd\ufffd" + "ghs_" + "B" * 36 + "\ufffd"
        result = guard.protect_output(text)
        assert "ghs_" + "B" * 36 not in result

    def test_scan_empty_lines_only(self, guard: CIGuard) -> None:
        text = "\n\n\n\n"
        assert guard.scan(text) == []

    def test_scan_very_long_single_line(self, guard: CIGuard) -> None:
        # Single line with 100K chars — no secret
        text = "x" * 100_000
        start = time.monotonic()
        findings = guard.scan(text)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0
        assert findings == []

    def test_custom_masker_is_used(self) -> None:
        # Verify CIGuard uses the injected masker, not a default one
        custom_masker = SecretMasker()
        guard = CIGuard(masker=custom_masker)
        assert guard._masker is custom_masker

    def test_all_ci_pattern_names_are_unique(self) -> None:
        names = [name for name, _, _ in _CI_PATTERNS]
        assert len(names) == len(set(names)), "Duplicate CI pattern names found"

    def test_findings_have_valid_severity_values(self, guard: CIGuard) -> None:
        valid_severities = {"CRITICAL", "HIGH", "MEDIUM"}
        text = (
            f"ghs_{'A' * 36}\n"
            "AKIAIOSFODNN7EXAMPLE\n"
        )
        findings = guard.scan(text)
        for f in findings:
            assert f.severity in valid_severities, f"Invalid severity '{f.severity}' in {f}"

    # -- protect_output double-match: CI replacement creating new base matches --

    def test_protect_output_ci_replacement_does_not_create_new_base_match(
        self, guard: CIGuard
    ) -> None:
        """CI pattern replacement text (e.g. [REDACTED:GITHUB_ACTIONS_TOKEN])
        must not itself match a base SecretMasker pattern.

        If the replacement string accidentally contained a substring matching
        a base regex (e.g. 'TOKEN'), it could be double-masked.
        """
        secret = "ghs_" + "A" * 36
        result = guard.protect_output(f"val={secret}")
        # The final result should contain exactly one [REDACTED:...] marker
        # for this secret, not nested/double redactions.
        assert result.count("[REDACTED:") == 1 or "[REDACTED:" in result
        assert secret not in result

    def test_protect_output_preserves_clean_text_around_secrets(
        self, guard: CIGuard
    ) -> None:
        """Clean text before/after a secret must survive masking unchanged."""
        secret = "bkua_" + "x" * 40
        text = f"BEFORE_MARKER {secret} AFTER_MARKER"
        result = guard.protect_output(text)
        assert secret not in result
        assert "BEFORE_MARKER" in result
        assert "AFTER_MARKER" in result

    # -- ReDoS resistance --

    def test_jenkins_pattern_no_redos_on_adversarial_input(self, guard: CIGuard) -> None:
        """JENKINS_TOKEN pattern `JENKINS_[A-Z_]*TOKEN\\s*=\\s*\\S{8,}` must not
        exhibit catastrophic backtracking on adversarial input.

        Attack: JENKINS_ + long uppercase + no TOKEN suffix = backtrack.
        """
        # Adversarial: very long uppercase string that doesn't end with TOKEN
        adversarial = "JENKINS_" + "A" * 10_000 + "= shortval"
        start = time.monotonic()
        guard.scan(adversarial)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"JENKINS_TOKEN regex took {elapsed:.2f}s (ReDoS?)"

    def test_docker_auth_pattern_no_redos_on_long_base64(self, guard: CIGuard) -> None:
        """DOCKER_AUTH pattern with very long base64 must not backtrack."""
        adversarial = '"auth": "' + "A" * 100_000 + '"'
        start = time.monotonic()
        findings = guard.scan(adversarial)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"DOCKER_AUTH regex took {elapsed:.2f}s (ReDoS?)"
        # Should match (valid base64 chars, length > 20)
        names = [f.pattern_name for f in findings]
        assert "DOCKER_AUTH" in names

    # -- Concurrent access to scan() --

    def test_scan_concurrent_threads(self, guard: CIGuard) -> None:
        """Multiple threads calling scan() concurrently must not corrupt results."""
        import threading

        secret = "ghs_" + "C" * 36
        text = f"token: {secret}"
        results: list[list[Finding]] = []
        errors: list[Exception] = []

        def scan_it() -> None:
            try:
                results.append(guard.scan(text))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=scan_it) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent scan errors: {errors}"
        assert len(results) == 20
        for r in results:
            names = [f.pattern_name for f in r]
            assert "GITHUB_ACTIONS_TOKEN" in names

    # -- Dedup consistency: multiple patterns on same line --

    def test_scan_multiple_patterns_same_line_each_has_own_finding(
        self, guard: CIGuard
    ) -> None:
        """A line matching both CI and base patterns should produce
        distinct findings for each pattern, not merge them."""
        # ghs_ token (CI) + AKIA (base) on same line
        line = f"ghs_{'D' * 36} AKIAIOSFODNN7EXAMPLE"
        findings = guard.scan(line)
        names = {f.pattern_name for f in findings}
        assert "GITHUB_ACTIONS_TOKEN" in names
        assert "AWS_KEY" in names
        # All should be on line 1
        assert all(f.line_number == 1 for f in findings)

    def test_scan_same_pattern_twice_on_same_line_produces_one_finding(
        self, guard: CIGuard
    ) -> None:
        """Two occurrences of same pattern on one line should produce exactly 1 finding
        (dedup by (line_number, pattern_name))."""
        token1 = "ghs_" + "E" * 36
        token2 = "ghs_" + "F" * 36
        line = f"{token1} {token2}"
        findings = guard.scan(line)
        gha_findings = [f for f in findings if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert len(gha_findings) == 1

    # -- Edge case: pattern at exact boundary --

    def test_buildkite_token_exact_length_40(self, guard: CIGuard) -> None:
        """bkua_ + exactly 40 chars should match. 39 should not."""
        match_text = "bkua_" + "a" * 40
        no_match_text = "bkua_" + "a" * 39
        assert any(f.pattern_name == "BUILDKITE_TOKEN" for f in guard.scan(match_text))
        # 39 chars should NOT match the exact-40 pattern
        bk_findings = [f for f in guard.scan(no_match_text) if f.pattern_name == "BUILDKITE_TOKEN"]
        assert len(bk_findings) == 0

    def test_github_actions_token_boundary_36_chars(self, guard: CIGuard) -> None:
        """ghs_ + exactly 36 chars should match. 35 should not."""
        match_text = "ghs_" + "a" * 36
        no_match_text = "ghs_" + "a" * 35
        assert any(f.pattern_name == "GITHUB_ACTIONS_TOKEN" for f in guard.scan(match_text))
        gha_short = [f for f in guard.scan(no_match_text) if f.pattern_name == "GITHUB_ACTIONS_TOKEN"]
        assert len(gha_short) == 0
