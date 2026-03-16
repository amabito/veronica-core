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
