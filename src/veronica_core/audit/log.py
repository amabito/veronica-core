"""Tamper-evident audit log for VERONICA Security Containment Layer.

Each log entry is a JSON object with a SHA256 hash chained from the
previous entry. Verifying the hash chain detects any modification,
insertion, or deletion of log records.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veronica_core.security.masking import SecretMasker


_GENESIS_HASH = "0" * 64


class AuditLog:
    """Append-only tamper-evident JSONL audit log.

    Each entry is a JSON object on its own line with the form::

        {
            "ts": "<ISO8601>",
            "event_type": "<str>",
            "data": {<masked>},
            "prev_hash": "<SHA256 hex of previous entry>",
            "hash": "<SHA256 hex of this entry>"
        }

    The hash covers (prev_hash + JSON dump of the entry without "hash").
    The first entry uses ``prev_hash = "0" * 64``.

    Thread-safe: all writes use an internal lock.

    Args:
        path: Path to the JSONL log file. Created if it does not exist.
        masker: Optional SecretMasker to redact secrets from log data.
    """

    def __init__(
        self,
        path: Path,
        masker: SecretMasker | None = None,
    ) -> None:
        self._path = path
        self._masker = masker
        self._lock = threading.Lock()
        self._prev_hash = self._load_last_hash()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, event_type: str, data: dict[str, Any]) -> None:
        """Append a new entry to the audit log.

        Args:
            event_type: Category string (e.g. "SHELL_EXECUTE", "DENY").
            data: Arbitrary dict payload. Secrets are masked before writing.
        """
        masked_data = self._masker.mask_dict(data) if self._masker else data
        entry = self._build_entry(event_type, masked_data)
        line = json.dumps(entry, separators=(",", ":")) + "\n"

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            self._prev_hash = entry["hash"]

    def verify_chain(self) -> bool:
        """Verify the hash chain of the log file.

        Returns:
            True if every entry's hash is consistent with its content
            and its ``prev_hash`` matches the previous entry's hash.
            Returns True for an empty log (vacuously valid).
        """
        if not self._path.exists():
            return True

        prev = _GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    return False

                stored_hash = entry.get("hash", "")
                if entry.get("prev_hash") != prev:
                    return False

                computed = self._compute_hash(entry)
                if computed != stored_hash:
                    return False

                prev = stored_hash

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_entry(
        self, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a new log entry dict including hash."""
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "data": data,
            "prev_hash": self._prev_hash,
        }
        entry["hash"] = self._compute_hash(entry)
        return entry

    @staticmethod
    def _compute_hash(entry: dict[str, Any]) -> str:
        """Compute SHA256 over prev_hash concatenated with the entry JSON.

        The "hash" key is excluded from the JSON to avoid circularity.
        """
        entry_without_hash = {k: v for k, v in entry.items() if k != "hash"}
        prev = entry_without_hash.get("prev_hash", _GENESIS_HASH)
        payload = prev + json.dumps(entry_without_hash, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _load_last_hash(self) -> str:
        """Read the last hash from an existing log, or return the genesis hash."""
        if not self._path.exists():
            return _GENESIS_HASH
        last_hash = _GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    last_hash = entry.get("hash", last_hash)
                except json.JSONDecodeError:
                    pass
        return last_hash
