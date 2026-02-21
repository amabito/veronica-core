#!/usr/bin/env python3
"""Generate Software Bill of Materials (SBOM) using stdlib importlib.metadata.

Zero external dependencies — uses only the Python standard library.

Usage:
    python tools/generate_sbom.py [output.json]

Output format:
    {
        "generated_at": "<ISO8601 UTC>",
        "packages": [
            {"name": str, "version": str, "deps": [str]},
            ...
        ]
    }

Packages are sorted alphabetically by name.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from importlib.metadata import packages_distributions, requires, version
from importlib.metadata import PackageNotFoundError
from pathlib import Path


def _safe_version(name: str) -> str:
    """Return installed version of *name*, or 'unknown' on error."""
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _safe_requires(name: str) -> list[str]:
    """Return dependency strings for *name*, or [] on error."""
    try:
        reqs = requires(name)
        return reqs if reqs is not None else []
    except PackageNotFoundError:
        return []


def generate_sbom(output_path: Path | None = None) -> dict:
    """Generate SBOM dict from installed packages.

    Args:
        output_path: If provided, write the JSON SBOM to this path.

    Returns:
        Dict with keys ``generated_at`` (ISO8601 str) and
        ``packages`` (list of dicts with ``name``, ``version``, ``deps``).
    """
    dist_map = packages_distributions()
    # Collect unique distribution names
    seen: set[str] = set()
    entries: list[dict] = []

    for _module, dist_names in dist_map.items():
        for dist_name in dist_names:
            if dist_name in seen:
                continue
            seen.add(dist_name)
            entries.append({
                "name": dist_name,
                "version": _safe_version(dist_name),
                "deps": _safe_requires(dist_name),
            })

    entries.sort(key=lambda e: e["name"].lower())
    # Sort deps lists for deterministic output
    for entry in entries:
        entry["deps"] = sorted(entry["deps"])

    sbom: dict = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "packages": entries,
    }

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(sbom, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return sbom


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sbom.json")
    sbom = generate_sbom(output)
    print(f"[OK] SBOM: {len(sbom['packages'])} packages -> {output}")
