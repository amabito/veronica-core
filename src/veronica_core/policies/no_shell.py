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
"""NoShellPolicy for VERONICA.

Blocks all shell and subprocess execution.  Does not restrict network
access or file I/O -- use ReadOnlyAssistantPolicy or NoNetworkPolicy
for those concerns.

Use this policy when the agent should be able to read and write files
and make network requests, but must not spawn processes.

Usage::

    from veronica_core.policies.no_shell import NoShellPolicy

    policy = NoShellPolicy(enabled=True)

    allowed, reason = policy.check_shell(["bash", "-c", "echo hello"])
    # allowed=False, reason="shell execution blocked by NoShellPolicy: 'bash'"

    allowed, reason = policy.check_shell(["ls", "-la"])
    # allowed=False -- NoShellPolicy blocks ALL shell commands

    # Allowlist for specific commands that are known to be safe
    policy = NoShellPolicy(enabled=True, allowlist={"ls", "cat", "echo"})
    allowed, reason = policy.check_shell(["ls", "-la"])
    # allowed=True
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


@dataclass
class NoShellPolicy:
    """Blocks all shell and subprocess execution.

    By default all shell commands are denied.  An optional allowlist
    permits specific command names when safe execution of known tools
    is required (e.g. read-only utilities like ``ls`` or ``cat``).

    When disabled, all check methods return (True, "policy disabled").

    Attributes:
        enabled: Whether the policy is active. Default False.
        allowlist: Set of command names (basename, no path) that are
            permitted despite the policy.
    """

    enabled: bool = False
    allowlist: frozenset[str] = field(default_factory=frozenset)

    def check_shell(
        self,
        args: list[str],
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether a shell command is allowed.

        All commands are blocked unless the executable basename appears
        in ``allowlist``.  The ``authority`` and ``side_effects`` parameters
        are accepted for API compatibility but do not affect the verdict --
        NoShellPolicy intent overrides both.

        Args:
            args: Command argument list. args[0] is the executable path
                or name.
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if not args:
            return True, "empty command allowed"
        cmd = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if cmd in self.allowlist:
            return True, f"shell command in allowlist: {cmd!r}"
        return False, f"shell execution blocked by NoShellPolicy: {cmd!r}"

    def create_event(
        self,
        reason: str,
        request_id: str | None = None,
    ) -> SafetyEvent:
        """Create a SafetyEvent for a blocked shell operation.

        Args:
            reason: Human-readable reason for the block.
            request_id: Optional request identifier for correlation.

        Returns:
            SafetyEvent with HALT decision.
        """
        return SafetyEvent(
            event_type="SHELL_POLICY_VIOLATION",
            decision=Decision.HALT,
            reason=reason,
            hook="NoShellPolicy",
            request_id=request_id,
        )
