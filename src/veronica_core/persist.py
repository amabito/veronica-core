"""VERONICA Persistence - State save/load for failsafe continuity."""

from __future__ import annotations
import json
import os
import warnings
from pathlib import Path
from typing import Optional
import logging

from veronica_core.state import VeronicaStateMachine

logger = logging.getLogger(__name__)


class VeronicaPersistence:
    """Persistence layer for VERONICA state.

    .. deprecated::
        Use :class:`veronica_core.backends.JSONBackend` (or
        :class:`veronica_core.backends.PersistenceBackend` for custom backends)
        instead. ``VeronicaPersistence`` will be removed in a future release.
    """

    DEFAULT_PATH = Path("data/state/veronica_state.json")

    def __init__(self, path: Optional[Path] = None):
        warnings.warn(
            "VeronicaPersistence is deprecated and will be removed in a future release. "
            "Use PersistenceBackend (veronica_core.backends) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Coerce str to Path so callers passing a string do not hit an
        # AttributeError on .parent.mkdir() below.
        if path is None:
            self.path: Path = self.DEFAULT_PATH
        else:
            self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, state: VeronicaStateMachine) -> bool:
        """Save state to disk. Returns True on success."""
        try:
            data = state.to_dict()

            # Atomic write: tmp -> rename (unpredictable name)
            import tempfile as _tempfile

            fd, tmp_name = _tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            fdopen_ok = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fdopen_ok = True
                    json.dump(data, f, indent=2)
                Path(tmp_name).replace(self.path)
            except BaseException:
                if not fdopen_ok:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                Path(tmp_name).unlink(missing_ok=True)
                raise
            logger.info(f"[VERONICA_PERSIST] State saved to {self.path}")
            return True

        except Exception as e:
            logger.error("[VERONICA_PERSIST] Save failed")
            logger.debug("[VERONICA_PERSIST] Save error detail: %s", e)
            return False

    def load(self) -> Optional[VeronicaStateMachine]:
        """Load state from disk. Returns None if file missing or invalid."""
        if not self.path.exists():
            logger.warning(
                f"[VERONICA_PERSIST] No state file at {self.path}, creating fresh state"
            )
            return None

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            state = VeronicaStateMachine.from_dict(data)
            logger.info(
                f"[VERONICA_PERSIST] State loaded from {self.path}: "
                f"{len(state.cooldowns)} active cooldowns, "
                f"{len(state.fail_counts)} fail counters"
            )
            return state

        except Exception as e:
            logger.error("[VERONICA_PERSIST] Load failed, creating fresh state")
            logger.debug("[VERONICA_PERSIST] Load error detail: %s", e)
            return None

    def backup(self) -> bool:
        """Create backup of current state file."""
        if not self.path.exists():
            return False

        try:
            import shutil
            from datetime import datetime, timezone

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = self.path.with_name(
                f"{self.path.stem}_backup_{timestamp}.json"
            )
            shutil.copy2(self.path, backup_path)
            logger.info(f"[VERONICA_PERSIST] Backup created: {backup_path}")
            return True

        except Exception as e:
            logger.error("[VERONICA_PERSIST] Backup failed")
            logger.debug("[VERONICA_PERSIST] Backup error detail: %s", e)
            return False
