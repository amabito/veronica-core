"""Tamper-evident audit log for VERONICA Security Containment Layer.

Each log entry is a JSON object with a SHA256 hash chained from the
previous entry. Verifying the hash chain detects any modification,
insertion, or deletion of log records.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from veronica_core.security.masking import SecretMasker  # noqa: E402


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

        # Build entry and update prev_hash inside the lock to prevent concurrent
        # writes from reading the same prev_hash and breaking the hash chain.
        with self._lock:
            entry = self._build_entry(event_type, masked_data)
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            self._prev_hash = entry["hash"]

    def append(self, payload: dict[str, Any]) -> None:
        """Append a raw payload dict as an audit entry.

        Uses the "event" key as event_type (defaults to "audit_event").
        The full payload is stored under the "data" field.

        Args:
            payload: Dict containing at minimum an "event" key.
        """
        event_type = payload.get("event", "audit_event")
        self.write(str(event_type), payload)

    def get_last_policy_version(self) -> int | None:
        """Backward scan of JSONL audit log to find last known policy version.

        Reads the file in reverse chunks without loading the entire file into
        RAM, making it safe for long-lived processes with millions of entries.

        Finds first:
        - ``{"event": "policy_checkpoint", "max_policy_version": N}`` → return N
        - ``{"event": "policy_version_accepted", "policy_version": N}`` → collect all, return max

        Returns:
            Last accepted policy version, or None if no policy version events found.
        """
        if not self._path.exists():
            return None

        max_accepted: int | None = None
        _CHUNK = 8192  # bytes per read when scanning backward

        try:
            with open(self._path, "rb") as f:
                f.seek(0, 2)  # seek to EOF
                file_size = f.tell()
                remainder = b""
                pos = file_size

                while pos > 0:
                    read_size = min(_CHUNK, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size) + remainder
                    lines = chunk.split(b"\n")
                    # The first element may be an incomplete line; carry it back.
                    remainder = lines[0]
                    # Process complete lines in reverse order (skip first partial).
                    for raw in reversed(lines[1:]):
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            logger.warning(
                                "audit_log: corrupted entry in %s — skipping line: %s",
                                self._path,
                                exc,
                            )
                            continue
                        data = entry.get("data", entry)
                        event = data.get("event") if isinstance(data, dict) else None
                        if event == "policy_checkpoint":
                            return int(data["max_policy_version"])
                        if event == "policy_version_accepted":
                            v = int(data.get("policy_version", 0))
                            if max_accepted is None or v > max_accepted:
                                max_accepted = v

                # Process leftover (the very first line of the file).
                if remainder.strip():
                    try:
                        entry = json.loads(remainder)
                        data = entry.get("data", entry)
                        event = data.get("event") if isinstance(data, dict) else None
                        if event == "policy_checkpoint":
                            return int(data["max_policy_version"])
                        if event == "policy_version_accepted":
                            v = int(data.get("policy_version", 0))
                            if max_accepted is None or v > max_accepted:
                                max_accepted = v
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

        except FileNotFoundError:
            return None

        return max_accepted

    def write_policy_checkpoint(self, policy_version: int) -> None:
        """Write a policy checkpoint entry recording the highest seen version.

        Args:
            policy_version: The current maximum accepted policy version.
        """
        self.append({"event": "policy_checkpoint", "max_policy_version": policy_version})

    def log_policy_version_accepted(self, policy_version: int, policy_path: str) -> None:
        """Log that a policy version was accepted.

        Args:
            policy_version: The accepted policy version number.
            policy_path: Path to the accepted policy file.
        """
        self.append({
            "event": "policy_version_accepted",
            "policy_version": policy_version,
            "policy_path": policy_path,
        })

    def log_policy_rollback(self, current_version: int, last_seen: int) -> None:
        """Log that a policy rollback was attempted.

        Args:
            current_version: The version number in the attempted policy.
            last_seen: The last accepted (higher) version number.
        """
        self.append({
            "event": "policy_rollback",
            "current_version": current_version,
            "last_seen_version": last_seen,
        })

    def log_sbom_diff(
        self,
        added: list[str],
        removed: list[str],
        changed: list[dict[str, str]],
        approved: bool,
    ) -> None:
        """Convenience method to log an SBOM diff event.

        Args:
            added: List of newly added package names.
            removed: List of removed package names.
            changed: List of dicts with keys ``name``, ``old_version``,
                ``new_version`` for each changed package.
            approved: Whether the diff was approved (e.g. via a valid token).
        """
        self.write(
            "SBOM_DIFF",
            {
                "added": added,
                "removed": removed,
                "changed": changed,
                "approved": approved,
            },
        )

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
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "audit_log: corrupted entry in %s — skipping line: %s",
                        self._path,
                        exc,
                    )
        return last_hash
