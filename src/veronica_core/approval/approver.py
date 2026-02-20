"""Approval system for VERONICA Security Containment Layer.

Provides HMAC-signed approval tokens for operations that require
explicit human approval before execution.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone


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
    """A signed approval token produced by CLIApprover.sign()."""

    request_id: str
    rule_id: str
    action: str
    args_hash: str
    timestamp: str
    signature: str   # HMAC-SHA256(secret_key, f"{rule_id}:{action}:{args_hash}:{timestamp}")


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
        """Sign *request* and return an ApprovalToken.

        The signature covers rule_id, action, args_hash, and timestamp.

        Args:
            request: ApprovalRequest to sign.

        Returns:
            ApprovalToken containing the HMAC-SHA256 signature.
        """
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

    def verify(self, token: ApprovalToken) -> bool:
        """Verify the HMAC signature of *token*.

        Args:
            token: Token to verify.

        Returns:
            True if the signature is valid, False otherwise.
        """
        message = f"{token.rule_id}:{token.action}:{token.args_hash}:{token.timestamp}"
        expected = hmac.new(
            self._key,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(token.signature, expected)

    def approve(self, token: ApprovalToken) -> bool:
        """Verify *token* and check that it is not older than 5 minutes.

        Args:
            token: Token to approve.

        Returns:
            True if the signature is valid and the token is fresh.
        """
        if not self.verify(token):
            return False

        try:
            issued_at = datetime.fromisoformat(token.timestamp)
        except ValueError:
            return False

        now = datetime.now(timezone.utc)
        # Ensure both datetimes are timezone-aware for comparison.
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
        age_seconds = (now - issued_at).total_seconds()
        return age_seconds <= _TOKEN_MAX_AGE_SECONDS
