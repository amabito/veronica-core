"""Tests for VeronicaPersistence (deprecated module, still in use).

Coverage:
- Happy path: save/load roundtrip, directory creation, backup
- Edge cases: missing file, corrupted JSON, binary garbage
- Adversarial: concurrent writes, read-only directory
- Deprecation: DeprecationWarning is emitted on construction
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from veronica_core.persist import VeronicaPersistence
from veronica_core.state import VeronicaStateMachine, VeronicaState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    fail_pair: str = "BTC/JPY",
    fail_count: int = 0,
    state: VeronicaState = VeronicaState.IDLE,
) -> VeronicaStateMachine:
    """Create a minimal VeronicaStateMachine for testing."""
    sm = VeronicaStateMachine()
    if fail_count:
        sm.fail_counts[fail_pair] = fail_count
    sm.current_state = state
    return sm


def _make_persistence(
    tmp_path: Path, filename: str = "state.json"
) -> VeronicaPersistence:
    """Create a VeronicaPersistence pointing at tmp_path, suppressing DeprecationWarning."""
    with pytest.warns(DeprecationWarning):
        return VeronicaPersistence(path=tmp_path / filename)


# ---------------------------------------------------------------------------
# TestVeronicaPersistence -- happy path and edge cases
# ---------------------------------------------------------------------------


class TestVeronicaPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        """State saved then loaded must be equivalent to the original."""
        p = _make_persistence(tmp_path)
        state = _make_state(fail_pair="ETH/JPY", fail_count=2)

        assert p.save(state) is True

        loaded = p.load()
        assert loaded is not None
        assert loaded.fail_counts.get("ETH/JPY") == 2
        assert loaded.current_state == VeronicaState.IDLE

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """save() must create nested directories that do not yet exist."""
        deep_path = tmp_path / "a" / "b" / "c" / "state.json"
        with pytest.warns(DeprecationWarning):
            p = VeronicaPersistence(path=deep_path)

        state = _make_state()
        assert p.save(state) is True
        assert deep_path.exists()

    def test_load_nonexistent_returns_none(self, tmp_path: Path) -> None:
        """load() on a missing file must return None (no exception)."""
        p = _make_persistence(tmp_path, filename="missing.json")
        # Remove file if it exists (it won't here, but be explicit)
        path = tmp_path / "missing.json"
        path.unlink(missing_ok=True)

        result = p.load()
        assert result is None

    def test_load_corrupted_json(self, tmp_path: Path) -> None:
        """load() on a file with invalid JSON must return None."""
        p = _make_persistence(tmp_path)
        (tmp_path / "state.json").write_text("not valid json {{{")

        result = p.load()
        assert result is None

    def test_backup_creation(self, tmp_path: Path) -> None:
        """backup() must create a timestamped copy of the state file."""
        p = _make_persistence(tmp_path)
        state = _make_state()
        p.save(state)

        assert p.backup() is True

        backups = list(tmp_path.glob("state_backup_*.json"))
        assert len(backups) == 1

    def test_backup_returns_false_when_no_file(self, tmp_path: Path) -> None:
        """backup() on a non-existent state file must return False."""
        p = _make_persistence(tmp_path, filename="absent.json")
        assert p.backup() is False

    def test_atomic_write_no_partial_file(self, tmp_path: Path) -> None:
        """No .tmp file should remain after a successful save."""
        p = _make_persistence(tmp_path)
        state = _make_state()
        p.save(state)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"

    def test_deprecation_warning_on_construction(self, tmp_path: Path) -> None:
        """VeronicaPersistence.__init__ must emit DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="deprecated"):
            VeronicaPersistence(path=tmp_path / "state.json")

    def test_save_preserves_fail_counts(self, tmp_path: Path) -> None:
        """All fail_counts for multiple pairs must survive roundtrip."""
        p = _make_persistence(tmp_path)
        state = _make_state()
        state.fail_counts["BTC/JPY"] = 3
        state.fail_counts["XRP/JPY"] = 1

        p.save(state)
        loaded = p.load()

        assert loaded is not None
        assert loaded.fail_counts["BTC/JPY"] == 3
        assert loaded.fail_counts["XRP/JPY"] == 1

    def test_save_overwrites_previous_state(self, tmp_path: Path) -> None:
        """Second save must replace first -- no stale data leaks through."""
        p = _make_persistence(tmp_path)

        state_v1 = _make_state(fail_pair="BTC/JPY", fail_count=5)
        p.save(state_v1)

        state_v2 = _make_state(fail_pair="BTC/JPY", fail_count=0)
        state_v2.fail_counts.clear()
        p.save(state_v2)

        loaded = p.load()
        assert loaded is not None
        assert loaded.fail_counts.get("BTC/JPY", 0) == 0

    def test_str_path_accepted(self, tmp_path: Path) -> None:
        """VeronicaPersistence must accept a str path without AttributeError."""
        str_path = str(tmp_path / "state.json")
        with pytest.warns(DeprecationWarning):
            p = VeronicaPersistence(path=str_path)  # type: ignore[arg-type]
        state = _make_state()
        assert p.save(state) is True
        assert p.load() is not None

    def test_default_path_used_when_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When path=None, DEFAULT_PATH is used (after monkeypatching cwd)."""
        monkeypatch.chdir(tmp_path)
        with pytest.warns(DeprecationWarning):
            p = VeronicaPersistence()
        assert p.path == VeronicaPersistence.DEFAULT_PATH


# ---------------------------------------------------------------------------
# TestAdversarialPersistence -- attacker mindset
# ---------------------------------------------------------------------------


class TestAdversarialPersistence:
    def test_concurrent_save_no_corruption(self, tmp_path: Path) -> None:
        """10 threads saving simultaneously must not produce a corrupted file."""
        p = _make_persistence(tmp_path)
        errors: list[Exception] = []

        def _save_worker(i: int) -> None:
            state = _make_state(fail_pair=f"PAIR-{i}", fail_count=i)
            try:
                p.save(state)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_save_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Exceptions during concurrent save: {errors}"

        # File must be valid JSON after the race
        result = p.load()
        assert result is not None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="chmod read-only semantics differ on Windows with admin rights",
    )
    def test_save_readonly_directory(self, tmp_path: Path) -> None:
        """save() into a read-only directory must return False, not raise."""
        p = _make_persistence(tmp_path)
        # Make the directory read-only after VeronicaPersistence was created
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            state = _make_state()
            result = p.save(state)
            assert result is False
        finally:
            # Restore permissions so pytest can clean up tmp_path
            os.chmod(tmp_path, stat.S_IRWXU)

    def test_load_binary_garbage(self, tmp_path: Path) -> None:
        """load() on a file with binary garbage must return None."""
        p = _make_persistence(tmp_path)
        (tmp_path / "state.json").write_bytes(b"\xff\xfe\x00\x01" * 64)

        result = p.load()
        assert result is None

    def test_load_truncated_json(self, tmp_path: Path) -> None:
        """load() on truncated JSON (partial write simulation) must return None."""
        p = _make_persistence(tmp_path)
        # Simulate truncated write mid-object
        (tmp_path / "state.json").write_text('{"cooldown_fails": 3, "cooldown_seconds"')

        result = p.load()
        assert result is None

    def test_load_json_wrong_type(self, tmp_path: Path) -> None:
        """load() on valid JSON that is not a dict must return None gracefully."""
        p = _make_persistence(tmp_path)
        (tmp_path / "state.json").write_text("[1, 2, 3]")

        result = p.load()
        # from_dict would raise TypeError on a list -- load() must catch it
        assert result is None

    def test_backup_does_not_overwrite_existing_backup(self, tmp_path: Path) -> None:
        """Two consecutive backup() calls with different timestamps create distinct files."""
        p = _make_persistence(tmp_path)
        p.save(_make_state())

        # First backup
        p.backup()
        first_backups = set(tmp_path.glob("state_backup_*.json"))

        # Manually rename to force a different name, then backup again
        for b in first_backups:
            b.rename(b.with_name("state_backup_19700101_000000.json"))

        p.backup()
        all_backups = list(tmp_path.glob("state_backup_*.json"))
        assert len(all_backups) == 2, (
            f"Expected 2 backups, found {len(all_backups)}: {all_backups}"
        )
