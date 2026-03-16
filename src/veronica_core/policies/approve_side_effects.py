# Copyright 2024 The VERONICA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ApproveSideEffectsPolicy for VERONICA.

Auto-approves read-only operations.  Requires explicit human approval
for write, network, and destructive operations before they proceed.

The policy classifies operations into two categories:

- **Read-only**: file reads, GET requests, non-destructive shell commands.
  These are auto-approved when ``enabled=True``.
- **Side-effecting**: file writes, non-GET HTTP, shell commands that modify
  state.  These require a prior call to ``request_approval(operation_id)``
  which returns a single-use nonce token.  Pass that token to
  ``record_approval(operation_id, token)`` to grant the approval.

Usage::

    from veronica_core.policies.approve_side_effects import ApproveSideEffectsPolicy

    policy = ApproveSideEffectsPolicy(enabled=True)

    # Read-only: auto-approved
    allowed, reason = policy.check_egress("https://api.example.com", method="GET")
    # allowed=True

    # Write: requires approval first
    allowed, reason = policy.check_egress("https://api.example.com/submit", method="POST")
    # allowed=False, reason="operation requires approval: ..."

    # Request a nonce, grant approval with that nonce, then retry
    token = policy.request_approval("POST:https://api.example.com/submit")
    policy.record_approval("POST:https://api.example.com/submit", token)
    allowed, reason = policy.check_egress("https://api.example.com/submit", method="POST")
    # allowed=True, approval consumed (single-use)
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field

from veronica_core.policies._policy_utils import _normalize_command_name
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision

_READ_ONLY_HTTP: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Shell commands that write, delete, transfer data, or execute arbitrary code.
# Versioned variants (python3.11, curl7, scp2 ...) are handled by
# _normalize_command_name() before lookup, so only canonical stems are listed.
_WRITE_SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "chmod",
        "chown",
        "dd",
        "mkfs",
        "fdisk",
        "mount",
        "umount",
        "kill",
        "pkill",
        "systemctl",
        "service",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",
        "pip",
        "npm",
        "yarn",
        "sudo",
        "su",
        "bash",
        "sh",
        "zsh",
        "fish",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "exec",
        "eval",
        # Data transfer / remote copy
        "curl",
        "wget",
        "scp",
        "rsync",
        "ftp",
        "sftp",
        # Stream multiplexer / file splitter
        "tee",
        # Text processing that can write files (awk -i, sed -i, etc.)
        "awk",
        "sed",
    }
)


@dataclass
class ApproveSideEffectsPolicy:
    """Requires human approval for write and destructive operations.

    Approvals are nonce-gated and single-use.  The caller must first obtain
    a token via ``request_approval(operation_id)`` and then pass that exact
    token to ``record_approval(operation_id, token)``.  This prevents an
    unrelated part of the codebase from pre-approving operations by guessing
    operation IDs.

    Thread-safe: the approval registry is protected by a lock.

    When disabled, all check methods return (True, "policy disabled").

    Attributes:
        enabled: Whether the policy is active. Default False.
    """

    enabled: bool = False
    # Maps operation_id -> nonce token for pending approvals.
    _approvals: dict[str, str] = field(default_factory=dict, repr=False)
    _approved: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def request_approval(self, operation_id: str) -> str:
        """Reserve an approval slot and return a single-use nonce token.

        The returned token must be passed to ``record_approval()`` to
        activate the approval.  Each call produces a new token, invalidating
        any previous unredeemed token for the same operation_id.

        Args:
            operation_id: Unique identifier for the operation to approve.
                Use a deterministic key such as "POST:https://example.com".

        Returns:
            A cryptographically random nonce string.
        """
        token = secrets.token_hex(16)
        with self._lock:
            self._approvals[operation_id] = token
        return token

    def record_approval(self, operation_id: str, token: str) -> None:
        """Activate a pending approval using the nonce issued by request_approval().

        Raises ValueError if the token does not match the pending nonce for
        this operation_id, preventing replay and cross-operation forgery.

        Args:
            operation_id: Unique identifier for the approved operation.
            token: The nonce returned by a prior call to request_approval().

        Raises:
            ValueError: If no pending approval exists for operation_id, or
                the token does not match.
        """
        with self._lock:
            expected = self._approvals.get(operation_id)
            if expected is None:
                raise ValueError(
                    f"no pending approval for operation {operation_id!r}; "
                    "call request_approval() first"
                )
            if not secrets.compare_digest(expected, token):
                # Discard the slot to prevent brute-force retry.
                del self._approvals[operation_id]
                raise ValueError(
                    f"invalid approval token for operation {operation_id!r}"
                )
            # Token is valid -- move from pending to approved set.
            del self._approvals[operation_id]
            self._approved.add(operation_id)

    def _consume_approval(self, operation_id: str) -> bool:
        """Attempt to consume an activated approval. Returns True if consumed.

        Only approvals that passed record_approval() are consumable.
        Pending (unredeemed) tokens cannot be consumed -- this prevents
        bypassing the approval flow by calling check_*() directly after
        request_approval() without record_approval().
        """
        with self._lock:
            if operation_id in self._approved:
                self._approved.discard(operation_id)
                return True
            return False

    def check_egress(self, url: str, method: str = "GET") -> tuple[bool, str]:
        """Check whether an outbound HTTP request is allowed.

        GET/HEAD/OPTIONS are auto-approved.  All other methods require
        a prior call to request_approval() + record_approval().

        Args:
            url: Target URL.
            method: HTTP method.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        upper = method.upper()
        if upper in _READ_ONLY_HTTP:
            return True, f"read-only HTTP {upper} auto-approved"
        operation_id = f"{upper}:{url}"
        if self._consume_approval(operation_id):
            return True, f"approved: {operation_id}"
        return (
            False,
            f"operation requires approval: {operation_id}",
        )

    def check_shell(self, args: list[str]) -> tuple[bool, str]:
        """Check whether a shell command is allowed.

        Read-only commands (not in the write list) are auto-approved.
        Write commands require prior approval via request_approval() +
        record_approval().

        Args:
            args: Command argument list.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if not args:
            return True, "empty command allowed"
        cmd = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        stem = _normalize_command_name(cmd)
        if stem not in _WRITE_SHELL_COMMANDS:
            return True, f"read-only shell command auto-approved: {cmd!r}"
        operation_id = f"SHELL:{stem}"
        if self._consume_approval(operation_id):
            return True, f"approved: {operation_id}"
        return False, f"operation requires approval: {operation_id}"

    def check_file_write(self, path: str) -> tuple[bool, str]:
        """Check whether a file write is allowed.

        All file writes require a prior call to request_approval() +
        record_approval().

        Args:
            path: File path being written.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        operation_id = f"WRITE:{path}"
        if self._consume_approval(operation_id):
            return True, f"approved: {operation_id}"
        return False, f"operation requires approval: {operation_id}"

    def pending_approvals(self) -> frozenset[str]:
        """Return the set of currently pending (unconsumed) approvals."""
        with self._lock:
            return frozenset(self._approvals)

    def create_event(
        self,
        reason: str,
        request_id: str | None = None,
    ) -> SafetyEvent:
        """Create a SafetyEvent for an operation pending approval.

        Args:
            reason: Human-readable reason for requiring approval.
            request_id: Optional request identifier for correlation.

        Returns:
            SafetyEvent with QUEUE decision (pending human approval).
        """
        return SafetyEvent(
            event_type="APPROVAL_REQUIRED",
            decision=Decision.QUEUE,
            reason=reason,
            hook="ApproveSideEffectsPolicy",
            request_id=request_id,
        )
