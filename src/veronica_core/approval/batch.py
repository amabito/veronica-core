"""Approval batching for VERONICA Security Containment Layer.

Groups repeated approval requests for the same operation so operators
receive one prompt instead of many, reducing approval fatigue.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BatchedRequest:
    """A group of approval requests sharing the same args_hash.

    Args:
        args_hash: SHA256 hex of ``"rule_id|action|arg0|arg1|..."``.
        rule_id: Policy rule that triggered the first request in the batch.
        action: Action type (e.g. "file_write").
        count: Number of requests accumulated so far.
    """

    args_hash: str
    rule_id: str
    action: str
    count: int = 1


# ---------------------------------------------------------------------------
# ApprovalBatcher
# ---------------------------------------------------------------------------


class ApprovalBatcher:
    """Groups approval requests by their args_hash to reduce operator fatigue.

    Two requests belong to the same batch when they share the same
    ``rule_id``, ``action``, and argument list.  The *args_hash* is
    computed as the SHA256 hex digest of ``"rule_id|action|arg0|arg1|..."``.

    Thread-safe.

    Args:
        on_batch_ready: Optional callback invoked every time a new batch
            group is started (i.e. the first request in a group).  Receives
            the :class:`BatchedRequest`.
    """

    def __init__(
        self,
        on_batch_ready: Callable[[BatchedRequest], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._batches: dict[str, BatchedRequest] = {}
        self._on_batch_ready = on_batch_ready

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def compute_args_hash(rule_id: str, action: str, args: list[str]) -> str:
        """Compute the batch key for a given rule/action/args triple.

        Args:
            rule_id: Policy rule identifier.
            action: Action type.
            args: Argument list.

        Returns:
            SHA256 hex digest of ``"rule_id|action|arg0|arg1|..."``.
        """
        parts = [rule_id, action] + list(args)
        payload = "|".join(parts)
        return hashlib.sha256(payload.encode()).hexdigest()

    def add(
        self,
        rule_id: str,
        action: str,
        args: list[str],
    ) -> BatchedRequest:
        """Add a request to the appropriate batch.

        If this is the first request for the given (rule_id, action, args)
        combination, a new :class:`BatchedRequest` is created (and
        ``on_batch_ready`` is called if provided).  Subsequent requests
        increment the batch counter.

        Args:
            rule_id: Policy rule that triggered the request.
            action: Action type.
            args: Arguments for the action.

        Returns:
            The :class:`BatchedRequest` (new or updated).
        """
        args_hash = self.compute_args_hash(rule_id, action, args)
        with self._lock:
            if args_hash in self._batches:
                self._batches[args_hash].count += 1
                return self._batches[args_hash]

            batch = BatchedRequest(
                args_hash=args_hash,
                rule_id=rule_id,
                action=action,
                count=1,
            )
            self._batches[args_hash] = batch

        # Notify outside lock to avoid holding lock during callback
        if self._on_batch_ready is not None:
            self._on_batch_ready(batch)
        return batch

    def get(self, args_hash: str) -> BatchedRequest | None:
        """Retrieve a batch by its args_hash.

        Args:
            args_hash: The hash key returned by :meth:`compute_args_hash`.

        Returns:
            The :class:`BatchedRequest` or None if not found.
        """
        with self._lock:
            return self._batches.get(args_hash)

    def clear(self, args_hash: str) -> None:
        """Remove a batch (e.g. after operator approval is received).

        Args:
            args_hash: The hash key of the batch to remove.
        """
        with self._lock:
            self._batches.pop(args_hash, None)

    def pending(self) -> list[BatchedRequest]:
        """Return all pending (unapproved) batches.

        Returns:
            List of :class:`BatchedRequest` objects in insertion order.
        """
        with self._lock:
            return list(self._batches.values())
