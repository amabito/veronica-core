#!/usr/bin/env python3
"""SBOM Diff Gate — detect changes between two SBOM snapshots.

Compares a baseline SBOM against a current SBOM, ignoring ``generated_at``
so that timestamp-only regenerations produce no diff.

Usage:
    python tools/sbom_diff.py baseline.json current.json [--secret <HMAC secret>]

Exit codes:
    0  — No differences (SBOMs are equivalent)
    1  — Differences found (caller should gate/approve)
    2  — Usage error (bad arguments, missing files, parse error)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PackageChange:
    """Describes a version change for a single package."""

    name: str
    old_version: str
    new_version: str


@dataclass
class SbomDiff:
    """Result of comparing two SBOMs.

    Attributes:
        added: Package names present in *current* but not in *baseline*.
        removed: Package names present in *baseline* but not in *current*.
        changed: Packages whose version changed between the two snapshots.
        is_clean: True when there are no additions, removals, or changes.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[PackageChange] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Return True when there are no differences."""
        return not (self.added or self.removed or self.changed)


# ---------------------------------------------------------------------------
# Core diff logic
# ---------------------------------------------------------------------------


def diff_sbom(baseline: dict[str, Any], current: dict[str, Any]) -> SbomDiff:
    """Compare two SBOM dicts, ignoring the ``generated_at`` field.

    Args:
        baseline: Parsed SBOM dict (the reference snapshot).
        current: Parsed SBOM dict (the new snapshot to compare against).

    Returns:
        :class:`SbomDiff` describing the differences.
    """
    baseline_pkgs: dict[str, str] = {
        pkg["name"]: pkg["version"] for pkg in baseline.get("packages", [])
    }
    current_pkgs: dict[str, str] = {
        pkg["name"]: pkg["version"] for pkg in current.get("packages", [])
    }

    baseline_names = set(baseline_pkgs)
    current_names = set(current_pkgs)

    added = sorted(current_names - baseline_names)
    removed = sorted(baseline_names - current_names)
    changed: list[PackageChange] = []
    for name in sorted(baseline_names & current_names):
        if baseline_pkgs[name] != current_pkgs[name]:
            changed.append(
                PackageChange(
                    name=name,
                    old_version=baseline_pkgs[name],
                    new_version=current_pkgs[name],
                )
            )

    return SbomDiff(added=added, removed=removed, changed=changed)


# ---------------------------------------------------------------------------
# Approval token
# ---------------------------------------------------------------------------


def _canonical_diff_json(diff: SbomDiff) -> bytes:
    """Return a deterministic JSON encoding of *diff* for HMAC computation."""
    payload = {
        "added": sorted(diff.added),
        "removed": sorted(diff.removed),
        "changed": [
            {
                "name": c.name,
                "old_version": c.old_version,
                "new_version": c.new_version,
            }
            for c in sorted(diff.changed, key=lambda c: c.name)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def compute_diff_token(diff: SbomDiff, secret: str) -> str:
    """Compute an HMAC-SHA256 approval token for *diff*.

    The token is derived from the canonical JSON encoding of the diff,
    keyed by *secret*.  Identical diffs with the same secret always produce
    the same token (deterministic).

    Args:
        diff: The :class:`SbomDiff` to sign.
        secret: HMAC key material (plain string, UTF-8 encoded internally).

    Returns:
        Hex-encoded HMAC-SHA256 digest string.
    """
    key = secret.encode()
    msg = _canonical_diff_json(diff)
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_approval_token(diff: SbomDiff, token: str, secret: str) -> bool:
    """Verify that *token* matches the expected HMAC for *diff*.

    Uses ``hmac.compare_digest`` to prevent timing attacks.

    Args:
        diff: The :class:`SbomDiff` to verify against.
        token: Hex-encoded HMAC-SHA256 token to check.
        secret: HMAC key material.

    Returns:
        True if the token is valid.
    """
    expected = compute_diff_token(diff, secret)
    return hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_sbom(path: Path) -> dict[str, Any]:
    """Load and parse an SBOM JSON file.

    Raises:
        SystemExit(2): On missing file or JSON parse error.
    """
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _print_diff(diff: SbomDiff) -> None:
    """Print a human-readable summary of *diff* to stdout."""
    if diff.is_clean:
        print("[OK] No SBOM differences detected.")
        return

    if diff.added:
        print(f"[ADDED] {len(diff.added)} package(s):")
        for name in diff.added:
            print(f"  + {name}")

    if diff.removed:
        print(f"[REMOVED] {len(diff.removed)} package(s):")
        for name in diff.removed:
            print(f"  - {name}")

    if diff.changed:
        print(f"[CHANGED] {len(diff.changed)} package(s):")
        for change in diff.changed:
            print(f"  ~ {change.name}: {change.old_version} -> {change.new_version}")


def main() -> None:
    """CLI entry point.

    Parses sys.argv, computes the diff, and exits with an appropriate code.

    Exit codes:
        0  — No differences
        1  — Differences found
        2  — Usage / parse error
    """
    args = sys.argv[1:]

    # Minimal arg parsing (no argparse dependency for zero-dep tool)
    if len(args) < 2:
        print(
            "Usage: sbom_diff.py baseline.json current.json [--secret <KEY>]",
            file=sys.stderr,
        )
        sys.exit(2)

    baseline_path = Path(args[0])
    current_path = Path(args[1])

    secret: str | None = None
    token: str | None = None
    i = 2
    while i < len(args):
        if args[i] == "--secret" and i + 1 < len(args):
            secret = args[i + 1]
            i += 2
        elif args[i] == "--token" and i + 1 < len(args):
            token = args[i + 1]
            i += 2
        else:
            print(f"ERROR: unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(2)

    baseline = _load_sbom(baseline_path)
    current = _load_sbom(current_path)

    diff = diff_sbom(baseline, current)
    _print_diff(diff)

    if diff.is_clean:
        sys.exit(0)

    # Differences found — check for pre-approved token
    if secret and token:
        if verify_approval_token(diff, token, secret):
            print("[OK] Diff approved by valid token.")
            sys.exit(0)
        else:
            print("[FAIL] Approval token is invalid.", file=sys.stderr)

    sys.exit(1)


if __name__ == "__main__":
    main()
