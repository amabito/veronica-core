"""Tests for release signing/verification tools (K-3).

All tests use subprocess to invoke the CLI tools so they exercise the
real exit-code contract without importing internals.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
SIGN_SCRIPT = TOOLS_DIR / "release_sign_policy.py"
VERIFY_SCRIPT = TOOLS_DIR / "verify_release.py"


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a tool script via the current Python interpreter.

    Args:
        script: Absolute path to the Python script.
        *args: Additional CLI arguments.
        env: Optional environment variable overrides (merged with current env).

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    import os

    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=merged_env,
    )


# ---------------------------------------------------------------------------
# verify_release.py
# ---------------------------------------------------------------------------


class TestVerifyRelease:
    def test_verify_release_passes(self) -> None:
        """verify_release.py exits 0 on the committed default.yaml + sig."""
        result = _run(VERIFY_SCRIPT)
        # Print output on failure to aid debugging.
        assert result.returncode == 0, (
            f"verify_release.py exited {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_verify_release_help(self) -> None:
        """verify_release.py --help exits 0 and prints usage."""
        result = _run(VERIFY_SCRIPT, "--help")
        assert result.returncode == 0, (
            f"--help exited {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        # argparse prints "usage:" to stdout.
        assert "usage" in result.stdout.lower()


# ---------------------------------------------------------------------------
# release_sign_policy.py
# ---------------------------------------------------------------------------


class TestReleaseSign:
    def test_release_sign_no_key(self) -> None:
        """release_sign_policy.py exits 1 when no private key is provided."""
        import os

        # Strip the env var if present so the tool cannot fall back on it.
        env_override = {k: v for k, v in os.environ.items()
                        if k != "VERONICA_PRIVATE_KEY_PEM"}

        result = subprocess.run(
            [sys.executable, str(SIGN_SCRIPT)],
            capture_output=True,
            text=True,
            env=env_override,
        )
        assert result.returncode == 1, (
            f"Expected exit 1 (no key), got {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        # Should print a helpful error message.
        combined = result.stdout + result.stderr
        assert "key" in combined.lower() or "error" in combined.lower()
