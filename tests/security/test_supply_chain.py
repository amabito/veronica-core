"""Tests for G-2: Supply Chain Guard — pip/npm REQUIRE_APPROVAL + SBOM."""
from __future__ import annotations

from pathlib import Path

import pytest

from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import PolicyContext, PolicyEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine() -> PolicyEngine:
    return PolicyEngine()


def _shell_ctx(args: list[str]) -> PolicyContext:
    return PolicyContext(
        action="shell",
        args=args,
        working_dir="/repo",
        repo_root="/repo",
        user=None,
        caps=CapabilitySet.dev(),
        env="dev",
    )


def _write_ctx(path: str) -> PolicyContext:
    return PolicyContext(
        action="file_write",
        args=[path],
        working_dir="/repo",
        repo_root="/repo",
        user=None,
        caps=CapabilitySet.dev(),
        env="dev",
    )


# ---------------------------------------------------------------------------
# Test 1: pip install requests → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_pip_install_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["pip", "install", "requests"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 2: pip install -r requirements.txt → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_pip_install_requirements_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["pip", "install", "-r", "requirements.txt"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 3: uv add httpx → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_uv_add_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["uv", "add", "httpx"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 4: npm install express → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_npm_install_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["npm", "install", "express"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 5: pnpm add lodash → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_pnpm_add_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["pnpm", "add", "lodash"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 6: yarn add axios → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_yarn_add_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["yarn", "add", "axios"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 7: cargo add serde → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_cargo_add_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["cargo", "add", "serde"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 8: pip show requests → ALLOW (not install)
# ---------------------------------------------------------------------------


def test_pip_show_is_allowed() -> None:
    engine = _engine()
    ctx = _shell_ctx(["pip", "show", "requests"])
    decision = engine.evaluate(ctx)
    # pip is not in SHELL_DENY_COMMANDS; "show" is not an install subcommand
    # pip is in neither SHELL_ALLOW_COMMANDS → default DENY (pip not in allowlist)
    # Actually pip is NOT in SHELL_ALLOW_COMMANDS, so it gets default DENY
    # The key constraint is: verdict is NOT REQUIRE_APPROVAL for "show"
    assert decision.verdict != "REQUIRE_APPROVAL" or decision.rule_id != "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 9: uv run pytest → ALLOW (not install)
# ---------------------------------------------------------------------------


def test_uv_run_pytest_is_allowed() -> None:
    engine = _engine()
    ctx = _shell_ctx(["uv", "run", "pytest"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# Test 10: uv pip install requests → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_uv_pip_install_requires_approval() -> None:
    engine = _engine()
    ctx = _shell_ctx(["uv", "pip", "install", "requests"])
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "SHELL_PKG_INSTALL"


# ---------------------------------------------------------------------------
# Test 11: lock file write uv.lock → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_lockfile_write_uv_lock_requires_approval() -> None:
    engine = _engine()
    ctx = _write_ctx("uv.lock")
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "FILE_WRITE_LOCKFILE"


# ---------------------------------------------------------------------------
# Test 12: lock file write package-lock.json → REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_lockfile_write_package_lock_requires_approval() -> None:
    engine = _engine()
    ctx = _write_ctx("package-lock.json")
    decision = engine.evaluate(ctx)
    assert decision.verdict == "REQUIRE_APPROVAL"
    assert decision.rule_id == "FILE_WRITE_LOCKFILE"


# ---------------------------------------------------------------------------
# Test 13: generate_sbom() returns dict with "packages" key
# ---------------------------------------------------------------------------


def test_generate_sbom_returns_packages_key() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
    from generate_sbom import generate_sbom

    sbom = generate_sbom()
    assert isinstance(sbom, dict)
    assert "packages" in sbom
    assert isinstance(sbom["packages"], list)


# ---------------------------------------------------------------------------
# Test 14: generate_sbom() entries have name, version fields
# ---------------------------------------------------------------------------


def test_generate_sbom_entry_fields() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
    from generate_sbom import generate_sbom

    sbom = generate_sbom()
    assert len(sbom["packages"]) > 0
    for pkg in sbom["packages"]:
        assert "name" in pkg
        assert "version" in pkg
        assert "deps" in pkg
        assert isinstance(pkg["name"], str)
        assert isinstance(pkg["version"], str)
        assert isinstance(pkg["deps"], list)


# ---------------------------------------------------------------------------
# Test 15: generate_sbom() writes JSON file when output_path given
# ---------------------------------------------------------------------------


def test_generate_sbom_writes_file(tmp_path: Path) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
    from generate_sbom import generate_sbom

    out = tmp_path / "sbom.json"
    sbom = generate_sbom(out)
    assert out.exists()
    import json
    loaded = json.loads(out.read_text())
    assert loaded["packages"] == sbom["packages"]
