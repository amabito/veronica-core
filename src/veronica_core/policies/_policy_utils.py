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
"""Shared utilities for VERONICA built-in policy packs."""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Command name normalization
# ---------------------------------------------------------------------------

# Strip trailing version suffixes so "python3.11" matches "python",
# "curl7.86" matches "curl", "node18" matches "node", etc.
#
# Strategy: take the leading run of ASCII letters (and hyphens, for
# commands like "apt-get").  Everything after the first digit or '.'
# that follows letters is treated as a version suffix.
#
# Examples:
#   "python3.11"  -> "python"
#   "python3"     -> "python"
#   "curl7.86"    -> "curl"
#   "wget2"       -> "wget"
#   "node18"      -> "node"
#   "apt-get"     -> "apt-get"   (hyphen preserved)
#   "bash"        -> "bash"      (no suffix)
#   "rm"          -> "rm"
_VERSION_SUFFIX_RE = re.compile(r"^([a-z][a-z\-]*)[\d.].*$")


def _normalize_command_name(cmd: str) -> str:
    """Strip version suffix from a command stem.

    Given a basename (already lowercased and .exe-stripped), remove any
    trailing version indicator so that denylist matching works against
    canonical names.

    Args:
        cmd: Lowercased command basename with .exe already removed.

    Returns:
        Canonical stem, e.g. "python" for "python3.11".
    """
    m = _VERSION_SUFFIX_RE.match(cmd)
    return m.group(1) if m else cmd


def _extract_command_stem(argv0: str) -> str:
    """Extract a normalized command stem from an argv[0] value.

    Performs three steps:
    1. Strip path prefix (handles both / and backslash separators)
    2. Lowercase and strip .exe suffix
    3. Strip version suffix via _normalize_command_name

    This consolidates the repeated pattern found across policy files:
        cmd = args[0].replace("\\\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        stem = _normalize_command_name(cmd)

    Args:
        argv0: The first element of a command argument list.

    Returns:
        Canonical command stem, e.g. "python" for "/usr/bin/python3.11.exe".
    """
    basename = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    return _normalize_command_name(basename)


# ---------------------------------------------------------------------------
# Shared command sets -- single source of truth for all policy packs
# ---------------------------------------------------------------------------

# Shell commands that write, delete, or execute arbitrary code.
# Used by ReadOnlyAssistantPolicy, ApproveSideEffectsPolicy, and others.
# Versioned variants (python3.11, curl7, scp2 ...) are handled by
# _extract_command_stem() before lookup, so only canonical stems are listed.
WRITE_SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        # File manipulation
        "rm",
        "rmdir",
        "mv",
        "cp",
        "chmod",
        "chown",
        "dd",
        # Disk / system
        "mkfs",
        "fdisk",
        "mount",
        "umount",
        # Process control
        "kill",
        "pkill",
        "systemctl",
        "service",
        # Package managers
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",
        "pip",
        "npm",
        "yarn",
        # Privilege escalation
        "sudo",
        "su",
        # Shells / interpreters
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
        "source",
        # Data transfer / remote copy
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "ftp",
        "sftp",
        # Stream / text processors that can write files
        "tee",
        "awk",
        "sed",
        # Scheduling
        "crontab",
        "at",
        "nohup",
    }
)

# Network-initiating shell commands.
# Used by NoNetworkPolicy.
NETWORK_SHELL_COMMANDS: frozenset[str] = frozenset(
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

# HTTP methods that are safe (read-only, no server-side state change).
# Everything NOT in this set is blocked -- positive allowlist, not denylist.
SAFE_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
