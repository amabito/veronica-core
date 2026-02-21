"""Tests for veronica_core.security.security_level (J-1)."""
from __future__ import annotations

import pytest

from veronica_core.security.security_level import (
    SecurityLevel,
    detect_security_level,
    get_security_level,
    reset_security_level,
    set_security_level,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton cache before and after each test."""
    reset_security_level()
    yield
    reset_security_level()


# ---------------------------------------------------------------------------
# detect_security_level
# ---------------------------------------------------------------------------

class TestDetectSecurityLevel:
    def test_explicit_dev(self, monkeypatch):
        monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "DEV")
        assert detect_security_level() == SecurityLevel.DEV

    def test_explicit_ci(self, monkeypatch):
        monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "CI")
        assert detect_security_level() == SecurityLevel.CI

    def test_explicit_prod(self, monkeypatch):
        monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "PROD")
        assert detect_security_level() == SecurityLevel.PROD

    def test_explicit_unknown_raises(self, monkeypatch):
        monkeypatch.setenv("VERONICA_SECURITY_LEVEL", "STAGING")
        with pytest.raises(ValueError, match="Unknown security level"):
            detect_security_level()

    def test_github_actions_detected_as_ci(self, monkeypatch):
        monkeypatch.delenv("VERONICA_SECURITY_LEVEL", raising=False)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert detect_security_level() == SecurityLevel.CI

    def test_generic_ci_var_detected(self, monkeypatch):
        monkeypatch.delenv("VERONICA_SECURITY_LEVEL", raising=False)
        monkeypatch.setenv("CI", "1")
        assert detect_security_level() == SecurityLevel.CI

    def test_fallback_to_dev_when_no_ci_vars(self, monkeypatch):
        monkeypatch.delenv("VERONICA_SECURITY_LEVEL", raising=False)
        for var in ("GITHUB_ACTIONS", "CI", "TRAVIS", "CIRCLECI", "GITLAB_CI",
                    "JENKINS_URL", "BITBUCKET_BUILD_NUMBER", "TF_BUILD"):
            monkeypatch.delenv(var, raising=False)
        assert detect_security_level() == SecurityLevel.DEV


# ---------------------------------------------------------------------------
# get_security_level / set_security_level / reset_security_level
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_caches_result(self, monkeypatch):
        monkeypatch.delenv("VERONICA_SECURITY_LEVEL", raising=False)
        for var in ("GITHUB_ACTIONS", "CI", "TRAVIS", "CIRCLECI", "GITLAB_CI",
                    "JENKINS_URL", "BITBUCKET_BUILD_NUMBER", "TF_BUILD"):
            monkeypatch.delenv(var, raising=False)
        level1 = get_security_level()
        level2 = get_security_level()
        assert level1 is level2

    def test_set_overrides_detection(self):
        set_security_level(SecurityLevel.PROD)
        assert get_security_level() == SecurityLevel.PROD

    def test_reset_clears_cache(self, monkeypatch):
        set_security_level(SecurityLevel.PROD)
        reset_security_level()
        monkeypatch.delenv("VERONICA_SECURITY_LEVEL", raising=False)
        for var in ("GITHUB_ACTIONS", "CI", "TRAVIS", "CIRCLECI", "GITLAB_CI",
                    "JENKINS_URL", "BITBUCKET_BUILD_NUMBER", "TF_BUILD"):
            monkeypatch.delenv(var, raising=False)
        assert get_security_level() == SecurityLevel.DEV
