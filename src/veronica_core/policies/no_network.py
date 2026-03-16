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

from veronica_core.policies._policy_utils import _normalize_command_name
from veronica_core.shield.event import SafetyEvent
from veronica_core.shield.types import Decision

# Network-initiating shell commands.
_NETWORK_SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "ftp",
        "sftp",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ping",
        "traceroute",
        "dig",
        "nslookup",
        "host",
        "whois",
        "nmap",
        "git",
    }
)


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

    def check_egress(self, url: str, method: str = "GET") -> tuple[bool, str]:
        """Check whether an outbound HTTP request is allowed.

        Args:
            url: Target URL.
            method: HTTP method (informational; all methods are blocked).

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if url in self.allowlist:
            return True, f"URL in allowlist: {url!r}"
        return False, f"outbound network blocked by NoNetworkPolicy: {url!r}"

    def check_shell(self, args: list[str]) -> tuple[bool, str]:
        """Check whether a shell command performs network I/O.

        Args:
            args: Command argument list. args[0] is the executable name.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.enabled:
            return True, "policy disabled"
        if not args:
            return True, "empty command allowed"
        cmd = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        stem = _normalize_command_name(cmd)
        if stem in _NETWORK_SHELL_COMMANDS:
            return False, f"network shell command blocked by NoNetworkPolicy: {cmd!r}"
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
