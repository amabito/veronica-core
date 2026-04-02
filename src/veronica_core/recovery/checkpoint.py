"""Immutable checkpoint and recovery for VERONICA containment state.

Snapshots critical containment state, signs with HMAC-SHA256, and allows
restoration on integrity failure. Fail-closed: no valid checkpoint returns
NO_CHECKPOINT so the caller must quarantine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RestoreResult(Enum):
    """Result of a checkpoint restoration attempt."""

    SUCCESS = "SUCCESS"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    NO_CHECKPOINT = "NO_CHECKPOINT"


@dataclass(frozen=True)
class ContainmentCheckpoint:
    """Signed snapshot of containment state.

    All fields are immutable after creation. The signature covers all
    other fields via canonical JSON -- any mutation is detectable.
    Use CheckpointManager to create and restore checkpoints.
    """

    policy_hash: str
    policy_epoch: int
    budget_remaining: float
    circuit_states: dict[str, str]
    risk_score: float
    timestamp: float
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy_epoch, int) or self.policy_epoch < 0:
            raise ValueError(
                f"policy_epoch must be a non-negative int, got {self.policy_epoch!r}"
            )
        if not isinstance(self.budget_remaining, (int, float)):
            raise ValueError(
                f"budget_remaining must be numeric, got {self.budget_remaining!r}"
            )
        if not isinstance(self.circuit_states, dict):
            raise ValueError(
                f"circuit_states must be a dict, got {type(self.circuit_states)!r}"
            )


class CheckpointManager:
    """Manages capture and restoration of signed containment checkpoints.

    Uses HMAC-SHA256 with a caller-supplied signing key.
    Ring buffer of max_checkpoints snapshots -- oldest dropped when full.
    restore() verifies signature before accepting the checkpoint.
    If no valid checkpoint exists, returns NO_CHECKPOINT (caller must quarantine).

    Thread-safe: all mutable state protected by threading.Lock.
    """

    def __init__(self, signing_key: bytes, max_checkpoints: int = 10) -> None:
        if not signing_key:
            raise ValueError("signing_key must be non-empty bytes")
        if max_checkpoints < 1:
            raise ValueError("max_checkpoints must be >= 1")
        self._key = signing_key
        self._checkpoints: deque[ContainmentCheckpoint] = deque(maxlen=max_checkpoints)
        self._lock = threading.Lock()

    def capture(self, ctx: Any) -> ContainmentCheckpoint:
        """Capture current state as a signed checkpoint.

        Extracts state from ctx using safe attribute access with defaults.
        Stores in ring buffer -- oldest entry dropped when at capacity.
        """
        policy_hash = str(getattr(ctx, "policy_hash", ""))
        policy_epoch = int(getattr(ctx, "policy_epoch", 0))
        budget_remaining = float(getattr(ctx, "budget_remaining", 0.0))

        circuit_states: dict[str, str] = {}
        raw_cs = getattr(ctx, "circuit_states", None)
        if isinstance(raw_cs, dict):
            circuit_states = {str(k): str(v) for k, v in raw_cs.items()}

        risk_score = float(getattr(ctx, "risk_score", 0.0))
        ts = time.time()

        payload: dict[str, Any] = {
            "policy_hash": policy_hash,
            "policy_epoch": policy_epoch,
            "budget_remaining": budget_remaining,
            "circuit_states": circuit_states,
            "risk_score": risk_score,
            "timestamp": ts,
        }
        sig = self._sign(payload)

        cp = ContainmentCheckpoint(
            policy_hash=policy_hash,
            policy_epoch=policy_epoch,
            budget_remaining=budget_remaining,
            circuit_states=circuit_states,
            risk_score=risk_score,
            timestamp=ts,
            signature=sig,
        )
        with self._lock:
            self._checkpoints.append(cp)
        return cp

    def restore(self, checkpoint: ContainmentCheckpoint) -> RestoreResult:
        """Verify signature, return result.

        Does not mutate ctx directly -- returns RestoreResult so the
        orchestrator decides how to apply state changes.
        """
        if not self._verify_signature(checkpoint):
            return RestoreResult.SIGNATURE_INVALID
        return RestoreResult.SUCCESS

    def latest_valid(self) -> ContainmentCheckpoint | None:
        """Return most recent checkpoint with valid signature, or None."""
        with self._lock:
            candidates = list(self._checkpoints)
        for cp in reversed(candidates):
            if self._verify_signature(cp):
                return cp
        return None

    def _sign(self, data: dict[str, Any]) -> str:
        """Compute HMAC-SHA256 over canonical JSON of data."""
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()

    def _verify_signature(self, checkpoint: ContainmentCheckpoint) -> bool:
        """Return True if checkpoint.signature matches expected HMAC."""
        payload: dict[str, Any] = {
            "policy_hash": checkpoint.policy_hash,
            "policy_epoch": checkpoint.policy_epoch,
            "budget_remaining": checkpoint.budget_remaining,
            "circuit_states": checkpoint.circuit_states,
            "risk_score": checkpoint.risk_score,
            "timestamp": checkpoint.timestamp,
        }
        expected = self._sign(payload)
        return hmac.compare_digest(expected, checkpoint.signature)
