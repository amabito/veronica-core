"""Security level detection for VERONICA containment layer.

Provides DEV / CI / PROD environment tiers.  The level governs
how strictly the PolicyEngine enforces signature and cryptography
requirements.
"""
from __future__ import annotations

import os
import threading
from enum import Enum, auto
from typing import Optional

# ---------------------------------------------------------------------------
# SecurityLevel enum
# ---------------------------------------------------------------------------

_CI_ENV_VARS: tuple[str, ...] = (
    "GITHUB_ACTIONS",
    "CI",
    "TRAVIS",
    "CIRCLECI",
    "GITLAB_CI",
    "JENKINS_URL",
    "BITBUCKET_BUILD_NUMBER",
    "TF_BUILD",
)

_ENV_VAR = "VERONICA_SECURITY_LEVEL"


class SecurityLevel(Enum):
    """Operational security tier.

    DEV  — local development; relaxed enforcement.
    CI   — continuous integration; strict enforcement.
    PROD — production; strict enforcement.
    """

    DEV = auto()
    CI = auto()
    PROD = auto()


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def detect_security_level() -> SecurityLevel:
    """Detect the current security level from the environment.

    Resolution order:
    1. ``VERONICA_SECURITY_LEVEL`` env var (``DEV``, ``CI``, or ``PROD``).
    2. Known CI environment variables (any truthy value).
    3. Falls back to ``DEV``.

    Returns:
        The detected :class:`SecurityLevel`.

    Raises:
        ValueError: If ``VERONICA_SECURITY_LEVEL`` is set to an unknown value.
    """
    explicit = os.environ.get(_ENV_VAR, "").strip().upper()
    if explicit:
        try:
            return SecurityLevel[explicit]
        except KeyError:
            valid = ", ".join(m.name for m in SecurityLevel)
            raise ValueError(
                f"Unknown security level '{explicit}' in {_ENV_VAR}. "
                f"Valid values: {valid}"
            )

    for var in _CI_ENV_VARS:
        if os.environ.get(var, ""):
            return SecurityLevel.CI

    return SecurityLevel.DEV


# ---------------------------------------------------------------------------
# Singleton state
# ---------------------------------------------------------------------------

_current_level: Optional[SecurityLevel] = None
_level_lock: threading.Lock = threading.Lock()


def get_security_level() -> SecurityLevel:
    """Return the process-wide security level, auto-detecting on first call.

    The result is cached after the first call.  Use
    :func:`reset_security_level` between tests.

    Thread-safe: reads and writes are protected by ``_level_lock``.

    Returns:
        The current :class:`SecurityLevel`.
    """
    global _current_level
    with _level_lock:
        if _current_level is None:
            _current_level = detect_security_level()
        return _current_level


def set_security_level(level: SecurityLevel) -> None:
    """Override the process-wide security level.

    Thread-safe: protected by ``_level_lock``.

    Args:
        level: The :class:`SecurityLevel` to set.
    """
    global _current_level
    with _level_lock:
        _current_level = level


def reset_security_level() -> None:
    """Clear the cached security level so it is re-detected on next access.

    Intended for use in test teardown.  Thread-safe: protected by
    ``_level_lock``.
    """
    global _current_level
    with _level_lock:
        _current_level = None
