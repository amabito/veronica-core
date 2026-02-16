"""VERONICA Persistence - State save/load for failsafe continuity."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
import logging

from veronica_core.state import VeronicaStateMachine

logger = logging.getLogger(__name__)


class VeronicaPersistence:
    """Persistence layer for VERONICA state."""

    DEFAULT_PATH = Path("data/state/veronica_state.json")

    def __init__(self, path: Optional[Path] = None):
        self.path = path or self.DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, state: VeronicaStateMachine) -> bool:
        """Save state to disk. Returns True on success."""
        try:
            data = state.to_dict()

            # Atomic write: tmp -> rename
            tmp_path = self.path.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=2)

            tmp_path.replace(self.path)
            logger.info(f"[VERONICA_PERSIST] State saved to {self.path}")
            return True

        except Exception as e:
            logger.error(f"[VERONICA_PERSIST] Save failed: {e}")
            return False

    def load(self) -> Optional[VeronicaStateMachine]:
        """Load state from disk. Returns None if file missing or invalid."""
        if not self.path.exists():
            logger.warning(f"[VERONICA_PERSIST] No state file at {self.path}, creating fresh state")
            return None

        try:
            with open(self.path, 'r') as f:
                data = json.load(f)

            state = VeronicaStateMachine.from_dict(data)
            logger.info(
                f"[VERONICA_PERSIST] State loaded from {self.path}: "
                f"{len(state.cooldowns)} active cooldowns, "
                f"{len(state.fail_counts)} fail counters"
            )
            return state

        except Exception as e:
            logger.error(f"[VERONICA_PERSIST] Load failed: {e}, creating fresh state")
            return None

    def backup(self) -> bool:
        """Create backup of current state file."""
        if not self.path.exists():
            return False

        try:
            import shutil
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.path.with_name(f"{self.path.stem}_backup_{timestamp}.json")
            shutil.copy2(self.path, backup_path)
            logger.info(f"[VERONICA_PERSIST] Backup created: {backup_path}")
            return True

        except Exception as e:
            logger.error(f"[VERONICA_PERSIST] Backup failed: {e}")
            return False
