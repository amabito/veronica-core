"""Mutual heartbeat sentinel for VERONICA containment.

Implements signed heartbeat protocol for in-process mutual monitoring.
This implements the PROTOCOL only -- process spawning is OS-specific
and out of scope for the kernel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum


class HeartbeatVerdict(Enum):
    """Result of heartbeat verification."""

    VALID = "VALID"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    STALE = "STALE"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class SignedHeartbeat:
    """Signed heartbeat for mutual watchdog protocol.

    Attributes:
        timestamp: Unix epoch float when heartbeat was created.
        nonce: UUID4 string for replay prevention.
        state_hash: SHA-256 hex digest of the serialized state_summary.
        signature: HMAC-SHA256 of "{timestamp}:{nonce}:{state_hash}".
    """

    timestamp: float
    nonce: str
    state_hash: str
    signature: str


class HeartbeatProtocol:
    """Signed heartbeat protocol -- create and verify heartbeats.

    Uses HMAC-SHA256 for signatures and nonce tracking to prevent replay.
    Freshness window: reject heartbeats older than 2x timeout_ms.

    Thread-safe: nonce set protected by threading.Lock.
    """

    def __init__(self, signing_key: bytes, timeout_ms: int = 5000) -> None:
        if not signing_key:
            raise ValueError("signing_key must be non-empty bytes")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be >= 1")
        self._key = signing_key
        self._timeout_s = timeout_ms / 1000.0
        self._freshness_window = 2.0 * self._timeout_s
        # Track last 1000 nonces to prevent replay
        self._seen_nonces: deque[str] = deque(maxlen=1000)
        self._nonce_set: set[str] = set()
        self._lock = threading.Lock()

    def create_heartbeat(self, state_summary: dict) -> SignedHeartbeat:
        """Create HMAC-signed heartbeat with current state."""
        ts = time.time()
        nonce = str(uuid.uuid4())
        canonical = json.dumps(state_summary, sort_keys=True, separators=(",", ":"))
        state_hash = hashlib.sha256(canonical.encode()).hexdigest()
        sig_data = f"{ts}:{nonce}:{state_hash}"
        signature = hmac.new(self._key, sig_data.encode(), hashlib.sha256).hexdigest()
        return SignedHeartbeat(
            timestamp=ts,
            nonce=nonce,
            state_hash=state_hash,
            signature=signature,
        )

    def verify_heartbeat(self, hb: SignedHeartbeat) -> HeartbeatVerdict:
        """Verify signature and freshness. Rejects stale or replayed heartbeats."""
        # Freshness check: reject if too old or from the future
        age = time.time() - hb.timestamp
        if age > self._freshness_window or age < 0:
            return HeartbeatVerdict.STALE

        # Replay prevention: nonce must not have been seen before
        with self._lock:
            if hb.nonce in self._nonce_set:
                return HeartbeatVerdict.STALE
            # When deque is at capacity, evict oldest from the lookup set
            if len(self._seen_nonces) == self._seen_nonces.maxlen:
                oldest = self._seen_nonces[0]
                self._nonce_set.discard(oldest)
            self._seen_nonces.append(hb.nonce)
            self._nonce_set.add(hb.nonce)

        # Signature verification
        sig_data = f"{hb.timestamp}:{hb.nonce}:{hb.state_hash}"
        expected = hmac.new(self._key, sig_data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hb.signature):
            return HeartbeatVerdict.INVALID_SIGNATURE

        return HeartbeatVerdict.VALID


class SentinelMonitor:
    """In-process side of mutual heartbeat monitoring.

    Sends heartbeats and validates received ones from a peer.
    If peer heartbeat is overdue by more than timeout, check_timeout() returns True.

    Thread-safe: all mutable state protected by threading.Lock.
    """

    def __init__(self, protocol: HeartbeatProtocol) -> None:
        self._protocol = protocol
        self._last_received_at: float = time.time()
        self._last_heartbeat: SignedHeartbeat | None = None
        self._lock = threading.Lock()

    def send(self, state_summary: dict | None = None) -> SignedHeartbeat:
        """Generate and return a signed heartbeat for the peer."""
        if state_summary is None:
            state_summary = {}
        hb = self._protocol.create_heartbeat(state_summary)
        with self._lock:
            self._last_heartbeat = hb
        return hb

    def receive(self, hb: SignedHeartbeat) -> HeartbeatVerdict:
        """Validate peer heartbeat. Updates last-received timestamp on VALID."""
        verdict = self._protocol.verify_heartbeat(hb)
        if verdict == HeartbeatVerdict.VALID:
            with self._lock:
                self._last_received_at = time.time()
        return verdict

    def check_timeout(self) -> bool:
        """Return True if peer heartbeat is overdue (timeout exceeded)."""
        with self._lock:
            elapsed = time.time() - self._last_received_at
        return elapsed > self._protocol._timeout_s

    @property
    def last_heartbeat(self) -> SignedHeartbeat | None:
        """Most recently sent heartbeat."""
        with self._lock:
            return self._last_heartbeat
