"""Audit chain for veronica-core safety events.

Each entry in the chain is hashed together with the previous entry's hash,
forming a hash chain (similar to a blockchain).  Any modification to a past
entry invalidates all subsequent hashes.

Security note: The hash chain uses plain SHA-256 without an HMAC key.
It proves *internal consistency* (accidental corruption is detectable) but
NOT *authenticity*.  An attacker with write access to the chain can forge
valid hashes for new entries.  Do not rely on this chain to detect deliberate
tampering by an adversary; it is suitable for audit trail consistency checks
only.

Usage::

    from veronica_core.compliance.audit_chain import AuditChain

    chain = AuditChain()
    chain.append({"event_type": "HALT", "reason": "budget exceeded"})
    chain.append({"event_type": "ALLOW", "reason": "within limits"})

    assert chain.verify()  # True if chain is intact
    entries = chain.entries()  # list of AuditEntry

The chain uses SHA-256 by default.  All entries are stored in-memory.
For persistence, use ``export_json()`` / ``from_json()`` to serialize
the chain to/from a JSON-compatible format.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


_GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    """Single entry in the audit chain.

    Attributes:
        sequence: Monotonically increasing index (0-based).
        timestamp: Unix epoch seconds when the entry was created.
        prev_hash: SHA-256 hex digest of the previous entry (or genesis hash).
        data: The event payload (arbitrary JSON-serializable dict).
        entry_hash: SHA-256 hex digest of this entry's canonical form.
    """

    sequence: int
    timestamp: float
    prev_hash: str
    data: dict[str, Any]
    entry_hash: str


def _canonical_bytes(
    sequence: int,
    timestamp: float,
    prev_hash: str,
    data: dict[str, Any],
) -> bytes:
    """Produce a deterministic byte representation for hashing."""
    canonical = json.dumps(
        {
            "sequence": sequence,
            "timestamp": timestamp,
            "prev_hash": prev_hash,
            "data": data,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return canonical.encode("utf-8")


def _compute_hash(
    sequence: int,
    timestamp: float,
    prev_hash: str,
    data: dict[str, Any],
) -> str:
    """Compute SHA-256 hex digest of an entry's canonical form."""
    raw = _canonical_bytes(sequence, timestamp, prev_hash, data)
    return hashlib.sha256(raw).hexdigest()


class AuditChain:
    """Thread-safe, append-only hash chain for audit events.

    Parameters
    ----------
    clock:
        Callable returning Unix epoch seconds.  Defaults to ``time.time``.
        Override for deterministic testing.
    """

    def __init__(self, *, clock: Any = None) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._clock = clock or time.time

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, data: dict[str, Any]) -> AuditEntry:
        """Append a new event to the chain and return the entry.

        The chain stores *data* as-is.  Callers are responsible for masking
        secrets **before** calling ``append()`` -- use ``SecretMasker`` from
        ``veronica_core.security.masking`` or the ``AuditLog`` wrapper which
        applies masking automatically.

        Args:
            data: JSON-serializable event payload (must be pre-masked).

        Returns:
            The newly created AuditEntry with computed hash.
        """
        with self._lock:
            prev_hash = (
                self._entries[-1].entry_hash if self._entries else _GENESIS_HASH
            )
            seq = len(self._entries)
            ts = self._clock()
            entry_hash = _compute_hash(seq, ts, prev_hash, data)
            entry = AuditEntry(
                sequence=seq,
                timestamp=ts,
                prev_hash=prev_hash,
                data=data,
                entry_hash=entry_hash,
            )
            self._entries.append(entry)
            return entry

    def verify(self) -> bool:
        """Verify the integrity of the entire chain.

        Returns True if all hashes are consistent.  Returns True for
        an empty chain.
        """
        with self._lock:
            return self._verify_unlocked()

    def verify_entry(self, index: int) -> bool:
        """Verify a single entry's hash and its link to the previous entry.

        Args:
            index: 0-based index of the entry to verify.

        Returns:
            True if the entry's hash is valid and links correctly.

        Raises:
            IndexError: If index is out of range.
        """
        with self._lock:
            if index < 0 or index >= len(self._entries):
                raise IndexError(f"Entry index {index} out of range")
            return self._verify_single(index)

    def entries(self) -> list[AuditEntry]:
        """Return a copy of all entries."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def export_json(self) -> list[dict[str, Any]]:
        """Export the chain as a JSON-serializable list of dicts."""
        with self._lock:
            return [asdict(e) for e in self._entries]

    @classmethod
    def from_json(
        cls, raw: list[dict[str, Any]], *, clock: Any = None
    ) -> "AuditChain":
        """Reconstruct an AuditChain from exported JSON.

        Raises ValueError if the imported chain fails verification.
        """
        chain = cls(clock=clock)
        for item in raw:
            entry = AuditEntry(
                sequence=item["sequence"],
                timestamp=item["timestamp"],
                prev_hash=item["prev_hash"],
                data=item["data"],
                entry_hash=item["entry_hash"],
            )
            chain._entries.append(entry)
        if not chain._verify_unlocked():
            raise ValueError("Imported audit chain fails integrity verification")
        return chain

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _verify_unlocked(self) -> bool:
        """Verify chain integrity (caller must hold lock)."""
        for i in range(len(self._entries)):
            if not self._verify_single(i):
                return False
        return True

    def _verify_single(self, index: int) -> bool:
        """Verify one entry (caller must hold lock)."""
        entry = self._entries[index]

        # Check prev_hash linkage
        expected_prev = (
            self._entries[index - 1].entry_hash if index > 0 else _GENESIS_HASH
        )
        if not hmac.compare_digest(entry.prev_hash, expected_prev):
            return False

        # Recompute and compare hash (constant-time to prevent timing side-channels)
        computed = _compute_hash(
            entry.sequence, entry.timestamp, entry.prev_hash, entry.data
        )
        return hmac.compare_digest(entry.entry_hash, computed)
