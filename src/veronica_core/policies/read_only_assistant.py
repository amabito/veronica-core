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
"""ReadOnlyAssistantPolicy for VERONICA.

Blocks shell commands, file writes, and non-GET network requests.
Allows file reads and GET requests.  Intended for assistants that
should observe but not modify system state.

Usage::

    from veronica_core.policies.read_only_assistant import ReadOnlyAssistantPolicy

    policy = ReadOnlyAssistantPolicy(enabled=True)

    # Check whether a shell command is allowed
    allowed, reason = policy.check_shell(["ls", "-la"])
    # allowed=True, reason="read-only shell command allowed"

    allowed, reason = policy.check_shell(["rm", "-rf", "/tmp/work"])
    # allowed=False, reason="shell write command blocked by ReadOnlyAssistantPolicy"

    # Check whether an HTTP request is allowed
    allowed, reason = policy.check_egress("https://api.example.com/data", method="GET")
    # allowed=True

    allowed, reason = policy.check_egress("https://api.example.com/data", method="POST")
    # allowed=False
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veronica_core.policies._policy_utils import (
    SAFE_HTTP_METHODS,
    WRITE_SHELL_COMMANDS,
    _extract_command_stem,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


@dataclass
class ReadOnlyAssistantPolicy:
    """Blocks write operations: shell commands, file writes, and non-GET HTTP.

    When disabled, all check methods return (True, "policy disabled").

    Attributes:
        enabled: Whether the policy is active. Default False.
        extra_denied_commands: Additional command names to block.
    """

    enabled: bool = False
    extra_denied_commands: frozenset[str] = field(default_factory=frozenset)

    def _denied_commands(self) -> frozenset[str]:
        return WRITE_SHELL_COMMANDS | self.extra_denied_commands

    def check_shell(
        self,
        args: list[str],
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether a shell command is allowed.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility but do not affect which commands are permitted --
        ReadOnlyAssistantPolicy intent overrides both for write commands.

        Args:
            args: Command argument list. args[0] is the executable name.
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if not args:
            return True, "empty command allowed"
        stem = _extract_command_stem(args[0])
        if stem in self._denied_commands():
            return (
                False,
                f"shell write command blocked by ReadOnlyAssistantPolicy: {stem!r}",
            )
        return True, "read-only shell command allowed"

    def check_egress(
        self,
        url: str,
        method: str = "GET",
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether an outbound HTTP request is allowed.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility but do not affect which methods are permitted.

        Args:
            url: Target URL.
            method: HTTP method (GET, POST, PUT, ...).
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        upper = method.upper()
        if upper not in SAFE_HTTP_METHODS:
            return (
                False,
                f"HTTP {upper} blocked by ReadOnlyAssistantPolicy"
                " (only GET/HEAD/OPTIONS allowed)",
            )
        return True, f"HTTP {upper} allowed"

    def check_file_write(
        self,
        path: str,
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether a file write operation is allowed.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility but do not affect the verdict -- file writes are always
        denied regardless of authority or profile.

        Args:
            path: File path being written.
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        return False, f"file write blocked by ReadOnlyAssistantPolicy: {path!r}"

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
            event_type="READ_ONLY_POLICY_VIOLATION",
            decision=Decision.HALT,
            reason=reason,
            hook="ReadOnlyAssistantPolicy",
            request_id=request_id,
        )
