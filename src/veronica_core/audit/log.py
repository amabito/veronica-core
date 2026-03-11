"""Audit log for VERONICA Security Containment Layer.

Each log entry is a JSON object with a SHA256 hash chained from the
previous entry.

Security note: The hash chain uses plain SHA-256 without an HMAC key.
It proves *internal consistency* only: accidental corruption (disk errors,
truncated writes) is detectable, but it does NOT prove authenticity.
An adversary with write access to the log file can compute valid hashes for
forged entries.  Do not rely on this chain to detect deliberate tampering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from veronica_core._utils import GENESIS_HASH  # noqa: E402
from veronica_core.security.masking import SecretMasker  # noqa: E402


_CHUNK = 8192  # bytes per read when scanning backward through JSONL files


def _scan_jsonl_reverse(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON entries from *path* in reverse order (last line first).

    Reads the file in backward chunks of ``_CHUNK`` bytes so that large logs
    are never fully loaded into memory.  Corrupted lines are skipped with a
    warning.  Stops cleanly at the beginning of the file.

    Args:
        path: Path to an existing JSONL file.

    Yields:
        Parsed ``dict`` for each non-empty line, from last to first.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        remainder = b""
        pos = file_size

        while pos > 0:
            read_size = min(_CHUNK, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + remainder
            lines = chunk.split(b"\n")
            # lines[0] may be an incomplete line at the chunk boundary; carry it back.
            remainder = lines[0]
            for raw in reversed(lines[1:]):
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "audit_log: corrupted entry in %s -- skipping line: %s",
                        path,
                        exc,
                    )

        # Process the leftover bytes (the very first line of the file).
        if remainder.strip():
            try:
                yield json.loads(remainder)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning(
                    "audit_log: corrupted entry in %s -- skipping line: %s",
                    path,
                    exc,
                )


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
        signer: Any = None,
    ) -> None:
        # H2: Validate signer has callable sign_bytes before accepting it.
        if signer is not None:
            sign_fn = getattr(signer, "sign_bytes", None)
            if not callable(sign_fn):
                raise TypeError(
                    f"AuditLog signer must have a callable sign_bytes() method, "
                    f"got {type(signer).__name__}"
                )
        self._path = path
        self._masker = masker
        self._signer = signer
        self._lock = threading.Lock()
        self._dir_created: bool = False
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
            # Sign the entry (without the hmac field) when a signer is present.
            # H2 guarantees self._signer has callable sign_bytes if not None.
            if self._signer is not None:
                entry_json = json.dumps(entry, sort_keys=True, separators=(",", ":"))
                entry["hmac"] = self._signer.sign_bytes(entry_json.encode("utf-8"))
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            if not self._dir_created:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._dir_created = True
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
        - ``{"event": "policy_checkpoint", "max_policy_version": N}`` -- return N
        - ``{"event": "policy_version_accepted", "policy_version": N}`` -- collect all, return max

        Returns:
            Last accepted policy version, or None if no policy version events found.
        """
        if not self._path.exists():
            return None

        max_accepted: int | None = None

        try:
            for entry in _scan_jsonl_reverse(self._path):
                data = entry.get("data", entry)
                event = data.get("event") if isinstance(data, dict) else None
                if event == "policy_checkpoint":
                    return int(data["max_policy_version"])
                if event == "policy_version_accepted":
                    v = int(data.get("policy_version", 0))
                    if max_accepted is None or v > max_accepted:
                        max_accepted = v
        except FileNotFoundError:
            return None

        return max_accepted

    def write_policy_checkpoint(self, policy_version: int) -> None:
        """Write a policy checkpoint entry recording the highest seen version.

        Args:
            policy_version: The current maximum accepted policy version.
        """
        self.append(
            {"event": "policy_checkpoint", "max_policy_version": policy_version}
        )

    def log_policy_version_accepted(
        self, policy_version: int, policy_path: str
    ) -> None:
        """Log that a policy version was accepted.

        Args:
            policy_version: The accepted policy version number.
            policy_path: Path to the accepted policy file.
        """
        self.append(
            {
                "event": "policy_version_accepted",
                "policy_version": policy_version,
                "policy_path": policy_path,
            }
        )

    def log_policy_rollback(self, current_version: int, last_seen: int) -> None:
        """Log that a policy rollback was attempted.

        Args:
            current_version: The version number in the attempted policy.
            last_seen: The last accepted (higher) version number.
        """
        self.append(
            {
                "event": "policy_rollback",
                "current_version": current_version,
                "last_seen_version": last_seen,
            }
        )

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

    def verify_chain(self, signer: Any = None) -> bool:
        """Verify the hash chain of the log file.

        Verifies both the SHA-256 hash chain and, if *signer* is provided,
        the HMAC signature on each entry that carries one.

        Args:
            signer: Optional signer with ``sign_bytes(data: bytes) -> str``
                    used to re-derive and compare HMAC values stored on entries.
                    Separate from ``self._signer`` to allow verification with
                    a different key than the one used for writing.

        Returns:
            True if every entry's hash is consistent with its content
            and its ``prev_hash`` matches the previous entry's hash.
            Also True if all present HMAC fields match when *signer* is given.
            Returns True for an empty log (vacuously valid).
        """
        if not self._path.exists():
            return True

        prev = GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
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

                # C3: When signer is provided, EVERY entry MUST carry an hmac field.
                # An attacker who deletes the hmac field must not pass verification.
                stored_hmac = entry.get("hmac")
                if signer is not None:
                    if stored_hmac is None:
                        return False
                    # Reconstruct the entry as it was before "hmac" was appended.
                    entry_without_hmac = {k: v for k, v in entry.items() if k != "hmac"}
                    entry_json = json.dumps(
                        entry_without_hmac, sort_keys=True, separators=(",", ":")
                    )
                    try:
                        expected_hmac = signer.sign_bytes(entry_json.encode("utf-8"))
                    except Exception:
                        return False
                    import hmac as _hmac

                    if not _hmac.compare_digest(expected_hmac, stored_hmac):
                        return False

                prev = stored_hash

        return True

    def write_governance_event(
        self,
        event_type: str,
        decision: str,
        reason_code: str,
        reason: str,
        audit_id: str,
        policy_hash: str,
        policy_epoch: int,
        issuer: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write a governance event (HALT/DEGRADE/QUARANTINE).

        Always signed when a signer is present on this AuditLog instance.

        Args:
            event_type: Event category (e.g. "GOVERNANCE_HALT").
            decision: Governance decision taken (e.g. "HALT", "DEGRADE").
            reason_code: Machine-readable reason code.
            reason: Human-readable explanation.
            audit_id: Unique identifier for this governance decision.
            policy_hash: Content hash of the policy that triggered this event.
            policy_epoch: Epoch of the policy at the time of the event.
            issuer: Identity of the system component issuing the event.
            metadata: Optional additional key-value pairs.
        """
        data: dict[str, Any] = {
            "decision": decision,
            "reason_code": reason_code,
            "reason": reason,
            "audit_id": audit_id,
            "policy_hash": policy_hash,
            "policy_epoch": policy_epoch,
            "issuer": issuer,
        }
        if metadata:
            data["metadata"] = metadata
        self.write(event_type, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_entry(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
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

        Both "hash" and "hmac" are excluded from the JSON to avoid
        circularity.  "hmac" is appended after hashing, so it must not
        be part of the hash input for verify_chain to remain consistent.
        """
        entry_without_hash = {
            k: v for k, v in entry.items() if k not in ("hash", "hmac")
        }
        prev = entry_without_hash.get("prev_hash", GENESIS_HASH)
        payload = prev + json.dumps(
            entry_without_hash, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _load_last_hash(self) -> str:
        """Read the last hash from an existing log, or return the genesis hash.

        Uses ``_scan_jsonl_reverse`` to avoid an O(n) forward scan for large logs.
        """
        if not self._path.exists():
            return GENESIS_HASH

        try:
            # Check for empty file before scanning.
            if self._path.stat().st_size == 0:
                return GENESIS_HASH

            for entry in _scan_jsonl_reverse(self._path):
                h = entry.get("hash")
                if h and isinstance(h, str) and len(h) == 64:
                    try:
                        int(h, 16)
                        return h
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass

        return GENESIS_HASH
