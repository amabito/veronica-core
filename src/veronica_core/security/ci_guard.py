"""CI-specific secret leak detection and protection.

Extends SecretMasker with CI-specific patterns for GitHub Actions,
GitLab CI, Docker, CircleCI, Jenkins, Artifactory, and Buildkite tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from veronica_core.security.masking import SecretMasker, _PATTERNS
from veronica_core.security.security_level import SecurityLevel, get_security_level


@dataclass(frozen=True)
class Finding:
    """A secret leak detected in CI output."""

    pattern_name: str
    line_number: int
    masked_snippet: str
    severity: str  # "CRITICAL" | "HIGH" | "MEDIUM"


# CI-specific patterns: (name, compiled_regex, severity)
# These supplement the 28 patterns already in SecretMasker._PATTERNS.
_CI_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("GITHUB_ACTIONS_TOKEN", re.compile(r"ghs_[A-Za-z0-9]{36,}"), "CRITICAL"),
    ("GITLAB_CI_TOKEN", re.compile(r"glcbt-[A-Za-z0-9\-_]{20,}"), "CRITICAL"),
    ("DOCKER_AUTH", re.compile(r'"auth"\s*:\s*"[A-Za-z0-9+/=]{20,}"'), "CRITICAL"),
    ("CIRCLECI_TOKEN", re.compile(r"circle-token\s*=\s*\S{20,}"), "HIGH"),
    ("JENKINS_TOKEN", re.compile(r"JENKINS_[A-Z_]*TOKEN\s*=\s*\S{8,}"), "HIGH"),
    ("ARTIFACTORY_TOKEN", re.compile(r"AKC[a-zA-Z0-9]{10,}"), "HIGH"),
    ("BUILDKITE_TOKEN", re.compile(r"bkua_[a-zA-Z0-9]{40}"), "HIGH"),
]


class CIGuard:
    """CI-specific secret leak detection and protection.

    Combines SecretMasker's 28 patterns with 7 CI-specific patterns
    for comprehensive secret leak detection in CI environments.
    """

    def __init__(self, masker: SecretMasker | None = None) -> None:
        self._masker = masker or SecretMasker()

    def scan(self, text: str) -> list[Finding]:
        """Scan text for leaked secrets.

        Returns a deduplicated list of findings, one per (line_number, pattern_name) pair.
        If multiple patterns match the same line, each distinct pattern yields one finding.
        """
        findings: list[Finding] = []
        # Track (line_number, pattern_name) to avoid duplicate findings.
        seen: set[tuple[int, str]] = set()
        lines = text.splitlines()

        for line_num, line in enumerate(lines, start=1):
            masked = self._masker.mask(line)

            # Check CI-specific patterns first (they take precedence for naming).
            for name, pattern, severity in _CI_PATTERNS:
                if pattern.search(line):
                    key = (line_num, name)
                    if key not in seen:
                        seen.add(key)
                        findings.append(
                            Finding(
                                pattern_name=name,
                                line_number=line_num,
                                masked_snippet=masked,
                                severity=severity,
                            )
                        )

            # Check existing SecretMasker patterns.
            for label, pattern in _PATTERNS:
                if pattern.search(line):
                    key = (line_num, label)
                    if key not in seen:
                        seen.add(key)
                        findings.append(
                            Finding(
                                pattern_name=label,
                                line_number=line_num,
                                masked_snippet=masked,
                                severity="HIGH",
                            )
                        )

        return findings

    def scan_file(self, path: Path) -> list[Finding]:
        """Scan a file for leaked secrets."""
        text = path.read_text(encoding="utf-8", errors="replace")
        return self.scan(text)

    def protect_output(self, text: str) -> str:
        """Mask all secrets in output text using both CI-specific and base patterns.

        CI-specific patterns are applied first so their labels take precedence
        over the generic base SecretMasker labels (e.g. GITHUB_ACTIONS_TOKEN
        wins over GITHUB_TOKEN for ``ghs_`` prefixed values).

        Returns the fully-masked text.
        """
        # Apply CI-specific patterns first (higher specificity).
        result = text
        for name, pattern, _severity in _CI_PATTERNS:
            result = pattern.sub(f"[REDACTED:{name}]", result)
        # Apply base SecretMasker patterns for any remaining secrets.
        result = self._masker.mask(result)
        return result

    @staticmethod
    def is_ci() -> bool:
        """Return True when running in CI or production environment."""
        level = get_security_level()
        return level in (SecurityLevel.CI, SecurityLevel.PROD)
