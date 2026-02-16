"""Tests for persistence backends."""

import pytest
import json
from pathlib import Path
import tempfile
import shutil

from veronica_core.backends import JSONBackend, MemoryBackend
from veronica_core.state import VeronicaStateMachine


class TestJSONBackend:
    """Test JSONBackend persistence."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for tests."""
        tmpdir = Path(tempfile.mkdtemp())
        yield tmpdir
        shutil.rmtree(tmpdir)

    def test_save_and_load(self, temp_dir):
        """Test basic save/load roundtrip."""
        path = temp_dir / "state.json"
        backend = JSONBackend(path)

        # Create test data
        sm = VeronicaStateMachine(cooldown_fails=3)
        sm.record_fail("task_1")
        data = sm.to_dict()

        # Save
        success = backend.save(data)
        assert success
        assert path.exists()

        # Load
        loaded_data = backend.load()
        assert loaded_data is not None
        assert loaded_data["cooldown_fails"] == 3
        assert loaded_data["fail_counts"] == {"task_1": 1}

    def test_atomic_write(self, temp_dir):
        """Test atomic write (tmp -> rename)."""
        path = temp_dir / "state.json"
        backend = JSONBackend(path)

        data = {"test": "value"}
        backend.save(data)

        # Verify .tmp file doesn't exist (should be renamed)
        tmp_path = path.with_suffix('.tmp')
        assert not tmp_path.exists()
        assert path.exists()

    def test_load_missing_file(self, temp_dir):
        """Test load returns None for missing file."""
        path = temp_dir / "nonexistent.json"
        backend = JSONBackend(path)

        loaded = backend.load()
        assert loaded is None

    def test_load_corrupted_file(self, temp_dir):
        """Test load handles corrupted JSON."""
        path = temp_dir / "corrupted.json"
        backend = JSONBackend(path)

        # Write corrupted JSON
        path.write_text("{ invalid json")

        # Should return None on error
        loaded = backend.load()
        assert loaded is None

    def test_backup(self, temp_dir):
        """Test backup creates timestamped copy."""
        path = temp_dir / "state.json"
        backend = JSONBackend(path)

        # Create initial state
        data = {"test": "value"}
        backend.save(data)

        # Create backup
        success = backend.backup()
        assert success

        # Check backup file exists
        backups = list(temp_dir.glob("state_backup_*.json"))
        assert len(backups) == 1
        assert backups[0].name.startswith("state_backup_")

        # Verify backup content
        backup_data = json.loads(backups[0].read_text())
        assert backup_data == data

    def test_auto_create_parent_directory(self, temp_dir):
        """Test backend creates parent directory if missing."""
        path = temp_dir / "nested" / "dir" / "state.json"
        backend = JSONBackend(path)

        # Parent should be created automatically
        assert path.parent.exists()


class TestMemoryBackend:
    """Test MemoryBackend (for testing)."""

    def test_save_and_load(self):
        """Test in-memory save/load."""
        backend = MemoryBackend()

        data = {"test": "value", "count": 123}
        success = backend.save(data)
        assert success

        loaded = backend.load()
        assert loaded == data

    def test_load_empty(self):
        """Test load returns None when no data saved."""
        backend = MemoryBackend()

        loaded = backend.load()
        assert loaded is None

    def test_isolation(self):
        """Test modifications to saved data don't affect stored copy."""
        backend = MemoryBackend()

        data = {"items": [1, 2, 3]}
        backend.save(data)

        # Modify original data
        data["items"].append(4)

        # Loaded data should be unchanged
        loaded = backend.load()
        assert loaded["items"] == [1, 2, 3]

    def test_no_persistence_across_instances(self):
        """Test MemoryBackend doesn't persist across instances."""
        backend1 = MemoryBackend()
        backend1.save({"test": "data"})

        backend2 = MemoryBackend()
        loaded = backend2.load()
        assert loaded is None
