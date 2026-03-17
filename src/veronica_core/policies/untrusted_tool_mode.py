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
"""UntrustedToolModePolicy for VERONICA.

Strictest built-in sandbox mode.  Denies shell execution, all network
access, and file writes.  File reads are restricted to an explicit
``allowed_read_paths`` sandbox; if no sandbox is configured a warning is
logged and reads are permitted (backwards-compatible behaviour).

Use this policy when running tools from untrusted sources: third-party
MCP servers, user-supplied plugins, or any tool whose implementation
cannot be audited before deployment.

This policy combines the restrictions of ReadOnlyAssistantPolicy and
NoNetworkPolicy into a single preset.

Usage::

    from veronica_core.policies.untrusted_tool_mode import UntrustedToolModePolicy

    # Strict mode -- only reads under /data/ are permitted
    policy = UntrustedToolModePolicy(
        enabled=True,
        allowed_read_paths=frozenset({"/data/"}),
    )

    # Shell execution -- always denied
    allowed, reason = policy.check_shell(["ls", "-la"])
    # allowed=False, reason="shell blocked in untrusted tool mode: 'ls'"

    # Network -- always denied
    allowed, reason = policy.check_egress("https://api.example.com")
    # allowed=False

    # File write -- always denied
    allowed, reason = policy.check_file_write("/tmp/output.txt")
    # allowed=False

    # File read inside sandbox -- allowed
    allowed, reason = policy.check_file_read("/data/config.json")
    # allowed=True

    # File read outside sandbox -- denied
    allowed, reason = policy.check_file_read("/etc/passwd")
    # allowed=False, reason="file read outside sandbox in untrusted tool mode: ..."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from veronica_core.policies._policy_utils import _extract_command_stem
from veronica_core.security.authority import is_policy_authority
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision

_LOG = logging.getLogger(__name__)


@dataclass
class UntrustedToolModePolicy:
    """Strictest sandbox: denies shell, network, file writes, and out-of-sandbox reads.

    When ``allowed_read_paths`` is provided, file reads are only permitted for
    paths that start with one of the configured prefixes.  If
    ``allowed_read_paths`` is ``None`` (the default), reads are unrestricted
    but a warning is emitted on the first read so operators know the sandbox
    is unconfigured.

    When disabled, all check methods return (True, "policy disabled").

    Attributes:
        enabled: Whether the policy is active. Default False.
        allowed_read_paths: Optional set of path prefixes that are readable.
            Pass ``frozenset({"/data/"})`` to restrict reads to /data/*.
            If None, reads are unrestricted (with a warning logged).
    """

    enabled: bool = False
    allowed_read_paths: frozenset[str] | None = field(default=None)
    _warned_unconfigured_reads: bool = field(default=False, repr=False, compare=False)
    _resolved_read_paths: tuple | None = field(
        default=None,
        repr=False,
        compare=False,
        init=False,
    )

    def _get_resolved_read_paths(self) -> tuple:
        """Return pre-resolved allowed_read_paths (cached after first call)."""
        if self._resolved_read_paths is not None:
            return self._resolved_read_paths
        from pathlib import Path

        resolved = tuple(Path(p).resolve() for p in (self.allowed_read_paths or ()))
        object.__setattr__(self, "_resolved_read_paths", resolved)
        return resolved

    def check_shell(
        self,
        args: list[str],
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Block all shell execution unconditionally.

        Only developer_policy or system_config authority sources may override
        this restriction.  All other sources (including user_input and
        agent_generated) are denied.  The ``side_effects`` parameter is
        accepted for API compatibility but does not affect the verdict --
        untrusted tool mode is the strictest sandbox.

        Args:
            args: Command argument list.
            authority: Optional AuthorityClaim. Only developer_policy /
                system_config sources bypass the block.
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple. Always (False, ...) when enabled, unless
            authority is developer_policy or system_config.
        """
        if not self.enabled:
            return True, "policy disabled"
        if is_policy_authority(authority):
            return True, "shell allowed: developer_policy/system_config override"
        if not args:
            return True, "empty command allowed"
        stem = _extract_command_stem(args[0])
        return False, f"shell blocked in untrusted tool mode: {stem!r}"

    def check_egress(
        self,
        url: str,
        method: str = "GET",
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Block all outbound network access unconditionally.

        Only developer_policy or system_config authority sources may override
        this restriction.  The ``side_effects`` parameter is accepted for API
        compatibility but does not affect the verdict.

        Args:
            url: Target URL.
            method: HTTP method (informational; all methods are blocked).
            authority: Optional AuthorityClaim. Only developer_policy /
                system_config sources bypass the block.
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple. Always (False, ...) when enabled, unless
            authority is developer_policy or system_config.
        """
        if not self.enabled:
            return True, "policy disabled"
        if is_policy_authority(authority):
            return True, "network allowed: developer_policy/system_config override"
        return False, f"network blocked in untrusted tool mode: {url!r}"

    def check_file_write(
        self,
        path: str,
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Block all file write operations unconditionally.

        Only developer_policy or system_config authority sources may override
        this restriction.  The ``side_effects`` parameter is accepted for API
        compatibility but does not affect the verdict.

        Args:
            path: File path being written.
            authority: Optional AuthorityClaim. Only developer_policy /
                system_config sources bypass the block.
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple. Always (False, ...) when enabled, unless
            authority is developer_policy or system_config.
        """
        if not self.enabled:
            return True, "policy disabled"
        if is_policy_authority(authority):
            return True, "file write allowed: developer_policy/system_config override"
        return False, f"file write blocked in untrusted tool mode: {path!r}"

    def check_file_read(
        self,
        path: str,
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether a file read is permitted.

        If ``allowed_read_paths`` is configured, only paths that start with
        one of the allowed prefixes are permitted.  Paths outside the sandbox
        (e.g. /etc/passwd, .env) are denied.

        If ``allowed_read_paths`` is None, reads are allowed but a one-time
        WARNING is emitted to alert operators that the read sandbox is not
        configured.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility with other policy check methods but do not affect the
        verdict -- untrusted tool mode sandbox is path-based only.

        Args:
            path: File path being read.
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"

        if self.allowed_read_paths is None:
            # No sandbox configured -- allow but warn once.
            if not self._warned_unconfigured_reads:
                _LOG.warning(
                    "UntrustedToolModePolicy: allowed_read_paths is not configured; "
                    "all file reads are permitted. Set allowed_read_paths to a "
                    "frozenset of path prefixes to restrict reads to a sandbox."
                )
                object.__setattr__(self, "_warned_unconfigured_reads", True)
            return (
                True,
                f"file read allowed in untrusted tool mode (no sandbox): {path!r}",
            )

        # Sandbox is configured -- only allow reads under an allowed prefix.
        # Use Path.resolve() to canonicalize (resolves .., symlinks) and
        # is_relative_to() to avoid prefix-string traversal bypasses.
        from pathlib import Path

        resolved = Path(path).resolve()
        for prefix in self._get_resolved_read_paths():
            if resolved.is_relative_to(prefix):
                return True, f"file read allowed in untrusted tool mode: {path!r}"

        return (
            False,
            f"file read outside sandbox in untrusted tool mode: {path!r}",
        )

    def create_event(
        self,
        reason: str,
        request_id: str | None = None,
    ) -> SafetyEvent:
        """Create a SafetyEvent for a blocked operation.

        Args:
            reason: Human-readable reason for the block.
            request_id: Optional request identifier for correlation.

        Returns:
            SafetyEvent with HALT decision.
        """
        return SafetyEvent(
            event_type="UNTRUSTED_TOOL_VIOLATION",
            decision=Decision.HALT,
            reason=reason,
            hook="UntrustedToolModePolicy",
            request_id=request_id,
        )
