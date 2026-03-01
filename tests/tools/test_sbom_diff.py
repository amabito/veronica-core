"""Tests for sbom_diff.py (I-3)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


# Make tools/ importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))

from sbom_diff import (  # noqa: E402
    PackageChange,
    SbomDiff,
    compute_diff_token,
    diff_sbom,
    verify_approval_token,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PKG_A_V1 = {"name": "pkg-a", "version": "1.0.0", "deps": []}
_PKG_A_V2 = {"name": "pkg-a", "version": "2.0.0", "deps": []}
_PKG_B = {"name": "pkg-b", "version": "0.5.0", "deps": []}
_PKG_C = {"name": "pkg-c", "version": "3.1.0", "deps": []}

_SECRET = "test-hmac-secret"


def _sbom(*pkgs: dict) -> dict:
    """Build a minimal SBOM dict with the given packages."""
    return {
        "schema_version": "1",
        "generated_at": "2026-02-21T00:00:00+00:00",
        "packages": list(pkgs),
    }


# ---------------------------------------------------------------------------
# diff_sbom: added packages
# ---------------------------------------------------------------------------


class TestDiffSbomAdded:
    def test_no_diff_on_identical_sboms(self) -> None:
        baseline = _sbom(_PKG_A_V1, _PKG_B)
        current = _sbom(_PKG_A_V1, _PKG_B)
        diff = diff_sbom(baseline, current)
        assert diff.is_clean
        assert diff.added == []
        assert diff.removed == []
        assert diff.changed == []

    def test_added_package_detected(self) -> None:
        baseline = _sbom(_PKG_A_V1)
        current = _sbom(_PKG_A_V1, _PKG_B)
        diff = diff_sbom(baseline, current)
        assert not diff.is_clean
        assert diff.added == ["pkg-b"]
        assert diff.removed == []
        assert diff.changed == []

    def test_multiple_added_packages(self) -> None:
        baseline = _sbom(_PKG_A_V1)
        current = _sbom(_PKG_A_V1, _PKG_B, _PKG_C)
        diff = diff_sbom(baseline, current)
        assert sorted(diff.added) == ["pkg-b", "pkg-c"]


# ---------------------------------------------------------------------------
# diff_sbom: removed packages
# ---------------------------------------------------------------------------


class TestDiffSbomRemoved:
    def test_removed_package_detected(self) -> None:
        baseline = _sbom(_PKG_A_V1, _PKG_B)
        current = _sbom(_PKG_A_V1)
        diff = diff_sbom(baseline, current)
        assert not diff.is_clean
        assert diff.removed == ["pkg-b"]
        assert diff.added == []

    def test_multiple_removed_packages(self) -> None:
        baseline = _sbom(_PKG_A_V1, _PKG_B, _PKG_C)
        current = _sbom(_PKG_A_V1)
        diff = diff_sbom(baseline, current)
        assert sorted(diff.removed) == ["pkg-b", "pkg-c"]


# ---------------------------------------------------------------------------
# diff_sbom: changed packages
# ---------------------------------------------------------------------------


class TestDiffSbomChanged:
    def test_version_change_detected(self) -> None:
        baseline = _sbom(_PKG_A_V1)
        current = _sbom(_PKG_A_V2)
        diff = diff_sbom(baseline, current)
        assert not diff.is_clean
        assert len(diff.changed) == 1
        change = diff.changed[0]
        assert isinstance(change, PackageChange)
        assert change.name == "pkg-a"
        assert change.old_version == "1.0.0"
        assert change.new_version == "2.0.0"
        assert diff.added == []
        assert diff.removed == []



# ---------------------------------------------------------------------------
# diff_sbom: combined scenarios
# ---------------------------------------------------------------------------


class TestDiffSbomCombined:
    def test_add_remove_change_together(self) -> None:
        baseline = _sbom(_PKG_A_V1, _PKG_B)
        current = _sbom(_PKG_A_V2, _PKG_C)
        diff = diff_sbom(baseline, current)
        assert not diff.is_clean
        assert diff.added == ["pkg-c"]
        assert diff.removed == ["pkg-b"]
        assert len(diff.changed) == 1
        assert diff.changed[0].name == "pkg-a"

    def test_generated_at_ignored(self) -> None:
        """Differences only in generated_at must not appear in diff."""
        baseline = {
            "schema_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "packages": [_PKG_A_V1],
        }
        current = {
            "schema_version": "1",
            "generated_at": "2026-02-21T12:34:56+00:00",
            "packages": [_PKG_A_V1],
        }
        diff = diff_sbom(baseline, current)
        assert diff.is_clean

    def test_empty_sboms_are_clean(self) -> None:
        diff = diff_sbom(_sbom(), _sbom())
        assert diff.is_clean


# ---------------------------------------------------------------------------
# compute_diff_token / verify_approval_token
# ---------------------------------------------------------------------------


class TestApprovalToken:
    def test_token_is_deterministic(self) -> None:
        """Same diff + same secret -> same token every time."""
        baseline = _sbom(_PKG_A_V1)
        current = _sbom(_PKG_A_V2)
        diff = diff_sbom(baseline, current)

        token1 = compute_diff_token(diff, _SECRET)
        token2 = compute_diff_token(diff, _SECRET)
        assert token1 == token2

    def test_token_is_hex_string(self) -> None:
        diff = SbomDiff(added=["pkg-x"], removed=[], changed=[])
        token = compute_diff_token(diff, _SECRET)
        # HMAC-SHA256 produces a 64-char hex string
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_verify_valid_token(self) -> None:
        diff = SbomDiff(added=["pkg-x"], removed=[], changed=[])
        token = compute_diff_token(diff, _SECRET)
        assert verify_approval_token(diff, token, _SECRET) is True

    def test_verify_wrong_token_fails(self) -> None:
        diff = SbomDiff(added=["pkg-x"], removed=[], changed=[])
        assert verify_approval_token(diff, "deadbeef" * 8, _SECRET) is False

    def test_verify_wrong_secret_fails(self) -> None:
        diff = SbomDiff(added=["pkg-x"], removed=[], changed=[])
        token = compute_diff_token(diff, _SECRET)
        assert verify_approval_token(diff, token, "wrong-secret") is False

    def test_clean_diff_token(self) -> None:
        """A clean (empty) diff also generates a valid, verifiable token."""
        diff = SbomDiff()
        token = compute_diff_token(diff, _SECRET)
        assert verify_approval_token(diff, token, _SECRET) is True

    def test_different_diffs_produce_different_tokens(self) -> None:
        diff_a = SbomDiff(added=["pkg-a"], removed=[], changed=[])
        diff_b = SbomDiff(added=["pkg-b"], removed=[], changed=[])
        assert compute_diff_token(diff_a, _SECRET) != compute_diff_token(diff_b, _SECRET)


# ---------------------------------------------------------------------------
# generate_sbom integration: schema_version and sorted deps
# ---------------------------------------------------------------------------


class TestGenerateSbomSchema:
    def test_generate_sbom_has_schema_version(self) -> None:
        """generate_sbom() output must include schema_version: '1'."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
        from generate_sbom import generate_sbom  # noqa: PLC0415

        sbom = generate_sbom()
        assert sbom.get("schema_version") == "1"

    def test_generate_sbom_packages_sorted(self) -> None:
        """Packages must be sorted by name (case-insensitive)."""
        from generate_sbom import generate_sbom  # noqa: PLC0415

        sbom = generate_sbom()
        names = [p["name"].lower() for p in sbom["packages"]]
        assert names == sorted(names)

    def test_generate_sbom_deps_sorted(self) -> None:
        """Each package's deps list must be sorted."""
        from generate_sbom import generate_sbom  # noqa: PLC0415

        sbom = generate_sbom()
        for pkg in sbom["packages"]:
            deps = pkg.get("deps", [])
            assert deps == sorted(deps), f"deps for {pkg['name']} not sorted"


# ---------------------------------------------------------------------------
# audit log integration
# ---------------------------------------------------------------------------


class TestAuditLogSbomDiff:
    def test_log_sbom_diff_writes_event(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog  # noqa: PLC0415

        log_path = tmp_path / "sbom_audit.jsonl"
        audit_log = AuditLog(log_path)

        audit_log.log_sbom_diff(
            added=["pkg-new"],
            removed=["pkg-old"],
            changed=[{"name": "pkg-changed", "old_version": "1.0", "new_version": "2.0"}],
            approved=False,
        )

        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        sbom_entries = [e for e in entries if e["event_type"] == "SBOM_DIFF"]
        assert len(sbom_entries) == 1
        data = sbom_entries[0]["data"]
        assert data["added"] == ["pkg-new"]
        assert data["removed"] == ["pkg-old"]
        assert data["approved"] is False

    def test_log_sbom_diff_approved(self, tmp_path: Path) -> None:
        from veronica_core.audit.log import AuditLog  # noqa: PLC0415

        log_path = tmp_path / "sbom_audit2.jsonl"
        audit_log = AuditLog(log_path)
        audit_log.log_sbom_diff(added=[], removed=[], changed=[], approved=True)

        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        assert entries[0]["data"]["approved"] is True
