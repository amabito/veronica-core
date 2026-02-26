"""Approval system for VERONICA Security Containment Layer.

Provides HMAC-signed approval tokens for operations that require
explicit human approval before execution.

Version history:
- v1 (sign): token covers rule_id:action:args_hash:timestamp
- v2 (sign_v2): token adds nonce + scope + expiry, with replay prevention
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time as _time
import uuid
import warnings
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.audit.log import AuditLog


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    """An unsigned request for operator approval."""

    rule_id: str
    action: str
    args_hash: str   # SHA256 hex of repr(args)
    timestamp: str   # ISO8601


@dataclass(frozen=True)
class ApprovalToken:
    """A signed approval token produced by CLIApprover.sign() or sign_v2().

    v1 fields (always present):
        request_id, rule_id, action, args_hash, timestamp, signature

    v2 additional fields (empty string = v1 token, no v2 checks):
        expiry  — ISO8601 expiry timestamp (timestamp + 5 min)
        nonce   — uuid4 hex; single-use (empty = v1 token)
        scope   — f"{action}:{args_hash}" (empty = v1 token)
    """

    request_id: str
    rule_id: str
    action: str
    args_hash: str
    timestamp: str
    signature: str   # HMAC-SHA256 payload (v1 or v2, see below)
    # v2 fields — default to empty string for backward compatibility
    expiry: str = ""
    nonce: str = ""
    scope: str = ""


# ---------------------------------------------------------------------------
# NonceRegistry — thread-safe single-use nonce tracker
# ---------------------------------------------------------------------------

_DEFAULT_NONCE_REGISTRY_MAX_SIZE = 10_000
# Nonces older than this are safe to evict: corresponding tokens are expired.
_NONCE_TTL_SECONDS = 5 * 60  # matches _TOKEN_MAX_AGE_SECONDS below


class NonceRegistry:
    """Thread-safe single-use nonce tracker.

    Once a nonce is consumed it cannot be reused, preventing replay attacks.

    Eviction is time-based: nonces older than ``_NONCE_TTL_SECONDS`` are
    removed before each lookup. This guarantees that an evicted nonce's
    corresponding token is already expired, so re-use would fail the token
    expiry check even if the nonce were somehow accepted.  The ``max_size``
    cap acts only as an additional memory safety net.

    Args:
        max_size: Maximum number of live nonces before oldest are dropped.
    """

    def __init__(self, max_size: int = _DEFAULT_NONCE_REGISTRY_MAX_SIZE) -> None:
        # Maps nonce → monotonic timestamp of insertion
        self._used: dict[str, float] = {}
        self._order: deque[str] = deque()  # insertion-order deque for O(1) eviction
        self._max_size = max_size
        self._lock = threading.Lock()

    def consume(self, nonce: str) -> bool:
        """Attempt to consume *nonce*.

        Returns True if the nonce is fresh (not previously seen) and records it.
        Returns False if the nonce was already consumed (replay detected).

        Args:
            nonce: Single-use nonce string to consume.

        Returns:
            True = fresh, False = replay.
        """
        with self._lock:
            now = _time.monotonic()
            # Evict expired nonces first so the size cap only removes live ones
            self._evict_expired(now)
            if nonce in self._used:
                return False
            self._used[nonce] = now
            self._order.append(nonce)
            # Safety net: cap memory if expiry-based eviction leaves too many
            while len(self._order) > self._max_size:
                oldest = self._order.popleft()
                self._used.pop(oldest, None)
            return True

    def _evict_expired(self, now: float) -> None:
        """Remove nonces older than the token TTL (caller must hold lock)."""
        cutoff = now - _NONCE_TTL_SECONDS
        while self._order:
            oldest = self._order[0]
            if self._used.get(oldest, now) < cutoff:
                self._order.popleft()
                self._used.pop(oldest, None)
            else:
                break

    def clear_expired(self) -> None:
        """Clear all recorded nonces (optional maintenance call)."""
        with self._lock:
            self._used.clear()
            self._order.clear()


# ---------------------------------------------------------------------------
# CLIApprover
# ---------------------------------------------------------------------------

_TOKEN_MAX_AGE_SECONDS = 5 * 60  # 5 minutes


class CLIApprover:
    """Creates, signs, and verifies approval tokens.

    If *secret_key* is None a random 32-byte key is generated per session.
    Tokens signed with an ephemeral key cannot be verified across process
    restarts; a warning is emitted in this case.

    Args:
        secret_key: Stable HMAC key (bytes). Use None only for testing.
    """

    def __init__(self, secret_key: bytes | None = None) -> None:
        if secret_key is None:
            warnings.warn(
                "CLIApprover: no secret_key provided; using ephemeral key. "
                "Tokens will not survive process restarts.",
                stacklevel=2,
            )
            self._key = os.urandom(32)
        else:
            self._key = secret_key
        self._nonce_registry = NonceRegistry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_request(
        self,
        rule_id: str,
        action: str,
        args: list[str],
    ) -> ApprovalRequest:
        """Create an unsigned ApprovalRequest for the given action.

        Args:
            rule_id: Policy rule that triggered the approval requirement.
            action: Action type (e.g. "file_write").
            args: Arguments for the action (will be hashed, not stored).

        Returns:
            ApprovalRequest with a SHA256 hash of the args list.
        """
        args_hash = hashlib.sha256(repr(args).encode()).hexdigest()
        timestamp = datetime.now(timezone.utc).isoformat()
        return ApprovalRequest(
            rule_id=rule_id,
            action=action,
            args_hash=args_hash,
            timestamp=timestamp,
        )

    def sign(self, request: ApprovalRequest) -> ApprovalToken:
        """Sign *request* using the v1 scheme (deprecated).

        The signature covers rule_id, action, args_hash, and timestamp.
        Prefer :meth:`sign_v2` for new code.

        Args:
            request: ApprovalRequest to sign.

        Returns:
            ApprovalToken containing the HMAC-SHA256 v1 signature.

        .. deprecated::
            Use :meth:`sign_v2` which adds nonce/scope/expiry for replay
            prevention.
        """
        warnings.warn(
            "CLIApprover.sign() is deprecated; use sign_v2() for replay prevention.",
            DeprecationWarning,
            stacklevel=2,
        )
        message = f"{request.rule_id}:{request.action}:{request.args_hash}:{request.timestamp}"
        signature = hmac.new(
            self._key,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return ApprovalToken(
            request_id=str(uuid.uuid4()),
            rule_id=request.rule_id,
            action=request.action,
            args_hash=request.args_hash,
            timestamp=request.timestamp,
            signature=signature,
        )

    def sign_v2(self, request: ApprovalRequest) -> ApprovalToken:
        """Sign *request* using the v2 scheme (recommended).

        The HMAC payload covers:
        ``"{rule_id}:{action}:{args_hash}:{timestamp}:{nonce}:{scope}"``

        Adds:
        - ``expiry``: timestamp + 5 minutes (ISO8601)
        - ``nonce``: uuid4 hex for single-use enforcement
        - ``scope``: ``f"{action}:{args_hash}"`` binds the token to the
          exact operation

        Args:
            request: ApprovalRequest to sign.

        Returns:
            ApprovalToken with v2 fields set.
        """
        nonce = uuid.uuid4().hex
        scope = f"{request.action}:{request.args_hash}"
        issued_at = datetime.fromisoformat(request.timestamp)
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
        expiry = (issued_at + timedelta(seconds=_TOKEN_MAX_AGE_SECONDS)).isoformat()

        message = (
            f"{request.rule_id}:{request.action}:{request.args_hash}"
            f":{request.timestamp}:{nonce}:{scope}"
        )
        signature = hmac.new(
            self._key,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return ApprovalToken(
            request_id=str(uuid.uuid4()),
            rule_id=request.rule_id,
            action=request.action,
            args_hash=request.args_hash,
            timestamp=request.timestamp,
            signature=signature,
            expiry=expiry,
            nonce=nonce,
            scope=scope,
        )

    def verify(self, token: ApprovalToken) -> bool:
        """Verify the HMAC signature of *token* (supports both v1 and v2).

        Args:
            token: Token to verify.

        Returns:
            True if the signature is valid, False otherwise.
        """
        # v2 token: nonce/scope fields present
        if token.nonce and token.scope:
            message = (
                f"{token.rule_id}:{token.action}:{token.args_hash}"
                f":{token.timestamp}:{token.nonce}:{token.scope}"
            )
        else:
            # v1 token: legacy HMAC payload
            message = f"{token.rule_id}:{token.action}:{token.args_hash}:{token.timestamp}"

        expected = hmac.new(
            self._key,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(token.signature, expected)

    def approve(
        self,
        token: ApprovalToken,
        audit_log: "AuditLog | None" = None,
    ) -> bool:
        """Verify *token* and enforce all v2 security checks.

        For v2 tokens, the checks are:
        1. HMAC signature valid
        2. Token not expired (uses ``token.expiry`` field if present,
           otherwise falls back to timestamp + 5 min for v1 tokens)
        3. Scope matches the token's action and args_hash
        4. Nonce is fresh (single-use; replay check)

        If *audit_log* is provided, writes an APPROVAL_GRANTED or
        APPROVAL_DENIED event on every call.

        Args:
            token: Token to approve.
            audit_log: Optional AuditLog to record the decision.

        Returns:
            True if all checks pass, False otherwise.
        """
        def _deny(reason: str) -> bool:
            if audit_log is not None:
                audit_log.write("APPROVAL_DENIED", {
                    "request_id": token.request_id,
                    "rule_id": token.rule_id,
                    "action": token.action,
                    "reason": reason,
                })
            return False

        # Step 1: Verify HMAC signature
        if not self.verify(token):
            return _deny("invalid_signature")

        # Step 2: Check expiry / age
        if token.expiry:
            # v2: use explicit expiry field
            try:
                expiry_at = datetime.fromisoformat(token.expiry)
            except ValueError:
                return _deny("invalid_expiry_format")
            if expiry_at.tzinfo is None:
                expiry_at = expiry_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry_at:
                return _deny("token_expired")
        else:
            # v1 fallback: check timestamp age
            try:
                issued_at = datetime.fromisoformat(token.timestamp)
            except ValueError:
                return _deny("invalid_timestamp_format")
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - issued_at).total_seconds()
            if age_seconds > _TOKEN_MAX_AGE_SECONDS:
                return _deny("token_expired")

        # v2-only checks
        if token.nonce and token.scope:
            # Step 3: Verify scope matches operation
            expected_scope = f"{token.action}:{token.args_hash}"
            if token.scope != expected_scope:
                return _deny("scope_mismatch")

            # Step 4: Consume nonce (replay prevention)
            if not self._nonce_registry.consume(token.nonce):
                return _deny("nonce_replayed")

        if audit_log is not None:
            audit_log.write("APPROVAL_GRANTED", {
                "request_id": token.request_id,
                "rule_id": token.rule_id,
                "action": token.action,
            })
        return True
