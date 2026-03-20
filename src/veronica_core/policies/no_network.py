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
"""NoNetworkPolicy for VERONICA.

Blocks all outbound network access.  Allows local filesystem operations,
shell commands that do not perform network I/O, and LLM calls that are
already proxied through the containment layer.

Use this policy in air-gapped environments or when data exfiltration
is a primary concern.

Usage::

    from veronica_core.policies.no_network import NoNetworkPolicy

    policy = NoNetworkPolicy(enabled=True)

    allowed, reason = policy.check_egress("https://api.example.com/data")
    # allowed=False, reason="outbound network blocked by NoNetworkPolicy"

    allowed, reason = policy.check_egress("https://api.example.com/data")
    # allowed=False for any URL

    # Allowlist for known-safe internal endpoints
    policy = NoNetworkPolicy(enabled=True, allowlist={"https://internal.corp/api"})
    allowed, reason = policy.check_egress("https://internal.corp/api")
    # allowed=True
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veronica_core.policies._policy_utils import (
    NETWORK_SHELL_COMMANDS,
    _extract_command_stem,
)
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision


@dataclass
class NoNetworkPolicy:
    """Blocks all outbound network access.

    When disabled, all check methods return (True, "policy disabled").

    Attributes:
        enabled: Whether the policy is active. Default False.
        allowlist: Set of exact URLs that are permitted despite the policy.
            Useful for known-safe internal endpoints.
    """

    enabled: bool = False
    allowlist: frozenset[str] = field(default_factory=frozenset)

    # Pre-lowercased allowlist for O(1) case-insensitive lookup.
    _allowlist_lower: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._allowlist_lower = frozenset(u.lower() for u in self.allowlist)

    def check_egress(
        self,
        url: str,
        method: str = "GET",
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether an outbound HTTP request is allowed.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility but do not affect the verdict -- NoNetworkPolicy intent
        overrides authority level and side-effect profiles.

        Args:
            url: Target URL.
            method: HTTP method (informational; all methods are blocked).
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile (ignored by this policy).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        # Case-insensitive comparison to prevent bypass via URL casing
        # (e.g. "HTTPS://Internal.Corp/api" vs "https://internal.corp/api").
        url_lower = url.lower()
        if url_lower in self._allowlist_lower:
            return True, f"URL in allowlist: {url!r}"
        return False, f"outbound network blocked by NoNetworkPolicy: {url!r}"

    def check_shell(
        self,
        args: list[str],
        authority: object = None,
        side_effects: object = None,
    ) -> tuple[bool, str]:
        """Check whether a shell command performs network I/O.

        The ``authority`` and ``side_effects`` parameters are accepted for API
        compatibility but do not affect the verdict -- NoNetworkPolicy intent
        overrides authority level.

        When *side_effects* reports OUTBOUND_NETWORK effects, the command is
        blocked even if its name is not in the known network command list.

        Args:
            args: Command argument list. args[0] is the executable name.
            authority: Optional AuthorityClaim (ignored by this policy).
            side_effects: Optional SideEffectProfile for profile-based detection.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if not args:
            return True, "empty command allowed"
        stem = _extract_command_stem(args[0])
        if stem in NETWORK_SHELL_COMMANDS:
            return False, f"network shell command blocked by NoNetworkPolicy: {stem!r}"
        # Side-effect aware: block any command whose profile reports network effects.
        if side_effects is not None and getattr(side_effects, "has_external", False):
            return False, f"network side effect blocked by NoNetworkPolicy: {stem!r}"
        return True, "non-network shell command allowed"

    def create_event(
        self,
        reason: str,
        request_id: str | None = None,
    ) -> SafetyEvent:
        """Create a SafetyEvent for a blocked network operation.

        Args:
            reason: Human-readable reason for the block.
            request_id: Optional request identifier for correlation.

        Returns:
            SafetyEvent with HALT decision.
        """
        return SafetyEvent(
            event_type="NETWORK_POLICY_VIOLATION",
            decision=Decision.HALT,
            reason=reason,
            hook="NoNetworkPolicy",
            request_id=request_id,
        )
