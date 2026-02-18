#!/usr/bin/env python3
"""Release quality gate -- fails CI when update leaks are detected.

Checks:
  A. Version consistency (pyproject.toml vs __init__.py vs git tag)
  B. README update freshness (ship readiness, docs links, demo links)
  C. Exports surface (public API symbols in __init__.py)
  D. Docs link reachability (README references existing files)

Usage:
  python tools/release_check.py --mode=pr       # push / PR checks
  python tools/release_check.py --mode=release --tag=v0.7.0  # release checks
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CORE_INIT = ROOT / "src" / "veronica_core" / "__init__.py"
SHIELD_INIT = ROOT / "src" / "veronica_core" / "shield" / "__init__.py"
README = ROOT / "README.md"


class CheckResult:
    """Accumulates pass/fail results with messages."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)
        print(f"  [PASS] {msg}")

    def fail(self, msg: str) -> None:
        self.failed.append(msg)
        print(f"  [FAIL] {msg}")

    @property
    def success(self) -> bool:
        return len(self.failed) == 0


def _extract_version_pyproject() -> str | None:
    """Extract version from pyproject.toml."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _extract_version_init(path: Path) -> str | None:
    """Extract __version__ from a Python __init__.py."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def check_version_consistency(result: CheckResult, tag: str | None) -> None:
    """A. Version consistency."""
    print("\n--- A. Version Consistency ---")

    pyproject_ver = _extract_version_pyproject()
    init_ver = _extract_version_init(CORE_INIT)

    if pyproject_ver is None:
        result.fail("Cannot parse version from pyproject.toml")
        return

    result.ok(f"pyproject.toml: {pyproject_ver}")

    if init_ver is None:
        result.fail("Cannot parse __version__ from veronica_core/__init__.py")
    elif init_ver != pyproject_ver:
        result.fail(
            f"veronica_core/__init__.py ({init_ver}) "
            f"!= pyproject.toml ({pyproject_ver})"
        )
    else:
        result.ok(f"veronica_core/__init__.py: {init_ver}")

    if tag is not None:
        tag_ver = tag.lstrip("v")
        if tag_ver != pyproject_ver:
            result.fail(
                f"Git tag ({tag_ver}) != pyproject.toml ({pyproject_ver})"
            )
        else:
            result.ok(f"Git tag: {tag_ver}")


def check_readme_freshness(result: CheckResult) -> None:
    """B. README update freshness."""
    print("\n--- B. README Freshness ---")

    if not README.exists():
        result.fail("README.md not found")
        return

    text = README.read_text(encoding="utf-8")

    # Ship Readiness section exists
    if re.search(r"Ship Readiness", text):
        result.ok("Ship Readiness section found")
    else:
        result.fail("Ship Readiness section missing from README")

    # Link to adaptive-control.md
    if "docs/adaptive-control.md" in text:
        result.ok("Link to docs/adaptive-control.md found")
    else:
        result.fail("Link to docs/adaptive-control.md missing from README")

    # Link to adaptive_demo.py
    if "adaptive_demo.py" in text:
        result.ok("Reference to adaptive_demo.py found")
    else:
        result.fail("Reference to adaptive_demo.py missing from README")

    # Version string in Ship Readiness matches pyproject
    pyproject_ver = _extract_version_pyproject()
    if pyproject_ver:
        pattern = rf"Ship Readiness.*v{re.escape(pyproject_ver)}"
        if re.search(pattern, text):
            result.ok(
                f"Ship Readiness references v{pyproject_ver}"
            )
        else:
            result.fail(
                f"Ship Readiness does not reference v{pyproject_ver}"
            )


def check_exports(result: CheckResult) -> None:
    """C. Public API surface check."""
    print("\n--- C. Exports Surface ---")

    required_in_shield = [
        "AdaptiveBudgetHook",
        "AdjustmentResult",
        "TimeAwarePolicy",
        "SafetyEvent",
        "TokenBudgetHook",
        "BudgetWindowHook",
        "InputCompressionHook",
    ]

    required_in_core = [
        "AdaptiveBudgetHook",
        "TimeAwarePolicy",
        "TokenBudgetHook",
        "BudgetWindowHook",
        "InputCompressionHook",
        "ShieldConfig",
    ]

    # Check shield/__init__.py
    if not SHIELD_INIT.exists():
        result.fail("shield/__init__.py not found")
        return

    shield_text = SHIELD_INIT.read_text(encoding="utf-8")
    for sym in required_in_shield:
        if sym in shield_text:
            result.ok(f"shield exports {sym}")
        else:
            result.fail(f"shield missing export: {sym}")

    # Check veronica_core/__init__.py
    if not CORE_INIT.exists():
        result.fail("veronica_core/__init__.py not found")
        return

    core_text = CORE_INIT.read_text(encoding="utf-8")
    for sym in required_in_core:
        if sym in core_text:
            result.ok(f"veronica_core exports {sym}")
        else:
            result.fail(f"veronica_core missing export: {sym}")


def check_docs_links(result: CheckResult) -> None:
    """D. Docs file reachability."""
    print("\n--- D. Docs Link Reachability ---")

    if not README.exists():
        result.fail("README.md not found")
        return

    text = README.read_text(encoding="utf-8")

    # Find all relative links in README (markdown links)
    links = re.findall(r'\[.*?\]\(((?!http)[^)]+)\)', text)

    if not links:
        result.ok("No relative links to check")
        return

    for link in links:
        # Strip anchor fragments
        path_str = link.split("#")[0]
        if not path_str:
            continue
        target = ROOT / path_str
        if target.exists():
            result.ok(f"Link target exists: {path_str}")
        else:
            result.fail(f"Link target missing: {path_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Release quality gate")
    parser.add_argument(
        "--mode",
        choices=["pr", "release"],
        default="pr",
        help="Check mode: pr (push/PR) or release (with tag)",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Git tag to verify (release mode only)",
    )
    args = parser.parse_args()

    tag = args.tag if args.mode == "release" else None

    result = CheckResult()

    check_version_consistency(result, tag)
    check_readme_freshness(result)
    check_exports(result)
    check_docs_links(result)

    print(f"\n{'='*50}")
    print(
        f"Results: {len(result.passed)} passed, "
        f"{len(result.failed)} failed"
    )

    if result.failed:
        print("\nFailed checks:")
        for msg in result.failed:
            print(f"  - {msg}")
        print("\nRelease check FAILED.")
        return 1

    print("\nRelease check PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
