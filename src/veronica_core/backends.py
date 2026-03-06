"""VERONICA Persistence Backend Interface - Pluggable storage."""

from __future__ import annotations

__all__ = [
    "PersistenceBackend",
    "JSONBackend",
    "MemoryBackend",
]
from abc import ABC, abstractmethod
from typing import Optional, Dict
import json
import copy
import threading
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class PersistenceBackend(ABC):
    """Abstract persistence backend for VERONICA state.

    Enables pluggable storage (JSON, Redis, PostgreSQL, etc.) without
    coupling the core state machine to any specific storage technology.
    """

    @abstractmethod
    def save(self, data: Dict) -> bool:
        """Save state data.

        Args:
            data: Serialized state dictionary (from VeronicaStateMachine.to_dict())

        Returns:
            True on success, False on failure
        """
        pass

    @abstractmethod
    def load(self) -> Optional[Dict]:
        """Load state data.

        Returns:
            Deserialized state dictionary, or None if no state exists
        """
        pass

    def backup(self) -> bool:
        """Create backup of current state (optional).

        Returns:
            True on success, False on failure (or if not supported)
        """
        return False  # Default: no-op


class JSONBackend(PersistenceBackend):
    """JSON file-based persistence backend (default).

    Uses atomic writes (tmp → rename) for crash safety.
    """

    def __init__(self, path: str | Path):
        """Initialize JSON backend.

        Args:
            path: Path to JSON state file (will be created if missing)
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._save_lock = threading.Lock()

    def save(self, data: Dict) -> bool:
        """Save state to JSON file with atomic write (thread-safe)."""
        with self._save_lock:
            try:
                # Atomic write: tmp -> rename
                tmp_path = self.path.with_suffix(".tmp")
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2)

                tmp_path.replace(self.path)
                logger.info(f"[JSONBackend] State saved to {self.path}")
                return True

            except Exception as e:
                logger.error(f"[JSONBackend] Save failed: {e}")
                return False

    def load(self) -> Optional[Dict]:
        """Load state from JSON file.

        M2: Performs basic schema validation after loading to guard against
        corrupted or tampered files. Returns None (and logs an error) if the
        loaded data is not a dict or lacks the expected top-level keys.
        """
        if not self.path.exists():
            logger.info(f"[JSONBackend] No state file at {self.path}")
            return None

        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[JSONBackend] Load failed: {e}")
            return None

        # M2: Basic schema check — data must be a dict with expected keys.
        if not isinstance(data, dict):
            logger.error(
                "[JSONBackend] Load failed: expected a JSON object, got %s",
                type(data).__name__,
            )
            return None
        expected_keys = {"current_state", "fail_counts", "cooldowns"}
        missing = expected_keys - data.keys()
        if missing:
            logger.warning(
                "[JSONBackend] Loaded state missing expected keys: %s. "
                "This may be an older format or a corrupted file.",
                sorted(missing),
            )

        logger.info(f"[JSONBackend] State loaded from {self.path}")
        return data

    def backup(self) -> bool:
        """Create timestamped backup of current state file."""
        if not self.path.exists():
            return False

        try:
            import shutil
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.path.with_name(
                f"{self.path.stem}_backup_{timestamp}.json"
            )
            shutil.copy2(self.path, backup_path)
            logger.info(f"[JSONBackend] Backup created: {backup_path}")
            return True

        except Exception as e:
            logger.error(f"[JSONBackend] Backup failed: {e}")
            return False


class MemoryBackend(PersistenceBackend):
    """In-memory persistence backend (for testing).

    Does NOT persist across process restarts.
    """

    def __init__(self):
        self._data: Optional[Dict] = None

    def save(self, data: Dict) -> bool:
        """Save to memory."""
        self._data = copy.deepcopy(data)
        logger.debug("[MemoryBackend] State saved to memory")
        return True

    def load(self) -> Optional[Dict]:
        """Load from memory."""
        if self._data is None:
            logger.debug("[MemoryBackend] No state in memory")
            return None
        logger.debug("[MemoryBackend] State loaded from memory")
        return copy.deepcopy(self._data)
