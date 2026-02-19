"""Tests for tools/release_check.py -- release quality gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "release_check.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


class TestPRMode:
    def test_script_runs(self) -> None:
        """Script executes without crashing."""
        result = _run("--mode=pr")
        # May pass or fail depending on repo state, but should not crash
        assert result.returncode in (0, 1)
        assert "Version Consistency" in result.stderr or "Version Consistency" in result.stdout

    def test_checks_all_sections(self) -> None:
        """All four check sections appear in output."""
        result = _run("--mode=pr")
        output = result.stdout + result.stderr
        assert "A. Version Consistency" in output
        assert "B. README Freshness" in output
        assert "C. Exports Surface" in output
        assert "D. Docs Link Reachability" in output

    def test_results_summary(self) -> None:
        """Results summary line is present."""
        result = _run("--mode=pr")
        output = result.stdout + result.stderr
        assert "Results:" in output
        assert "passed" in output


class TestReleaseMode:
    def test_release_mode_with_tag(self) -> None:
        """Release mode accepts --tag argument."""
        result = _run("--mode=release", "--tag=v0.7.1")
        output = result.stdout + result.stderr
        assert "Version Consistency" in output

    def test_release_mode_wrong_tag(self) -> None:
        """Release mode fails on mismatched tag."""
        result = _run("--mode=release", "--tag=v99.99.99")
        output = result.stdout + result.stderr
        assert "FAIL" in output
        assert result.returncode == 1
