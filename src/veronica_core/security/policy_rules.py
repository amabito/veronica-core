"""Policy rule constants, data types, helpers, and evaluators.

Extracted from ``policy_engine.py`` to keep file sizes manageable.
All public symbols are re-exported from ``policy_engine.py`` for
backward compatibility.
"""

from __future__ import annotations

import collections
import fnmatch
import math
import os
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from veronica_core.security.capabilities import Capability, CapabilitySet

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

ActionLiteral = Literal["shell", "file_read", "file_write", "net", "git", "browser"]

SHELL_DENY_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "del",
        "format",
        "reg",
        "schtasks",
        "wmic",
        "powershell",
        "cmd",
        "certutil",
        "bitsadmin",
        "curl",
        "wget",
        "scp",
        "sftp",
    }
)

SHELL_DENY_OPERATORS: tuple[str, ...] = (
    "|",
    ">",
    ">>",
    "2>",
    "&&",
    ";",
    # Command substitution: $(...) and backtick forms allow arbitrary sub-shell execution
    # even in arguments passed to allowlisted commands (e.g. "echo $(cat /etc/passwd)").
    "$(",
    "`",
    # Newline injection: allows multi-command payloads embedded in a single argument string.
    "\n",
    # Null-byte injection: terminates strings in C-based shells, bypasses string comparisons.
    "\x00",
)

FILE_READ_DENY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/AppData/Local/Google/Chrome/User Data/**",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.kube/**",
    # Credential / secret files (E-2 expansion)
    "**/.npmrc",
    "**/.pypirc",
    "**/.netrc",
    "**/*id_rsa*",
    "**/*id_ed25519*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    # Linux procfs: exposes environment variables, command-line args,
    # and file descriptors (includes secrets passed via env or CLI).
    "/proc/self/environ",
    "/proc/self/cmdline",
    "/proc/*/environ",
    "/proc/*/cmdline",
)

NET_ALLOWLIST_HOSTS: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "raw.githubusercontent.com",
        "registry.npmjs.org",
    }
)

NET_DENY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})

NET_URL_MAX_LENGTH: int = 2048

NET_ENTROPY_THRESHOLD: float = 4.5
NET_ENTROPY_MIN_LEN: int = 20

# Per-host path prefix allowlist (only these prefixes are permitted for GET)
NET_PATH_ALLOWLIST: dict[str, list[str]] = {
    "pypi.org": ["/pypi/", "/simple/"],
    "files.pythonhosted.org": ["/packages/"],
    "github.com": ["/"],
    "raw.githubusercontent.com": ["/"],
    "registry.npmjs.org": ["/"],
}

_RE_BASE64 = re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$")
_RE_HEX = re.compile(r"^[0-9a-fA-F]{32,}$")

GIT_DENY_SUBCMDS: frozenset[str] = frozenset({"push", "workflow", "release", "tag"})

FILE_WRITE_APPROVAL_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    "package.json",
    ".git/hooks/**",
    "*.ps1",
    "*.bat",
    "*.sh",
    "policies/*.yaml",
)

SHELL_ALLOW_COMMANDS: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "uv",
        "npm",
        "pnpm",
        "cargo",
        "go",
        "cmake",
    }
)

SHELL_DENY_EXEC_FLAGS: dict[str, frozenset[str]] = {
    "cmake": frozenset({"-P", "-E"}),
    "make": frozenset({"--eval", "-f"}),
    "go": frozenset({"run", "generate", "tool", "env"}),
}

_UVR_INLINE_EXEC_FLAGS: frozenset[str] = frozenset({"-c", "--eval"})

_PYTHON_MODULE_PKG_MANAGERS: frozenset[str] = frozenset({"pip", "pip3", "ensurepip"})

FILE_COUNT_APPROVAL_THRESHOLD = 20

SHELL_PKG_INSTALL_APPROVAL: tuple[tuple[str, frozenset[str]], ...] = (
    ("pip", frozenset({"install", "download"})),
    ("pip3", frozenset({"install", "download"})),
    ("npm", frozenset({"install", "add", "i"})),
    ("pnpm", frozenset({"install", "add", "i"})),
    ("yarn", frozenset({"install", "add"})),
    ("cargo", frozenset({"add", "install"})),
)

_UV_INSTALL_SUBCMDS: frozenset[str] = frozenset({"add", "pip"})
_UV_PIP_INSTALL_SUBCMDS: frozenset[str] = frozenset({"install", "download"})

FILE_WRITE_LOCKFILE_PATTERNS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
    "requirements.txt",
    "requirements-*.txt",
)

SHELL_CREDENTIAL_DENY: tuple[tuple[str, frozenset[str]], ...] = (
    ("git", frozenset({"credential", "credentials"})),
    ("gh", frozenset({"auth", "token", "secret"})),
    ("npm", frozenset({"token", "login", "logout", "adduser", "set-script"})),
    ("pip", frozenset({"config"})),
)


@dataclass(frozen=True)
class ExecPolicyContext:
    """Immutable snapshot describing an action to be evaluated by PolicyEngine.

    Named ExecPolicyContext (not PolicyContext) to avoid collision with
    runtime_policy.PolicyContext, which carries LLM call context (cost, steps).
    """

    action: ActionLiteral
    args: list[str]
    working_dir: str
    repo_root: str
    user: str | None
    caps: CapabilitySet
    env: str  # "dev" | "ci" | "audit" | "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


# Backward-compatible alias -- prefer ExecPolicyContext in new code.
PolicyContext = ExecPolicyContext


@dataclass(frozen=True)
class ExecPolicyDecision:
    """Result of a PolicyEngine evaluation.

    Named ExecPolicyDecision (not PolicyDecision) to avoid collision with
    runtime_policy.PolicyDecision, which carries allowed/denied LLM call outcome.
    """

    verdict: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    rule_id: str
    reason: str
    risk_score_delta: int  # 0=safe, 1-5=moderate, 6-10=critical


# Backward-compatible alias -- prefer ExecPolicyDecision in new code.
PolicyDecision = ExecPolicyDecision


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    """Return True if *path* matches any glob pattern.

    Normalizes separators to forward slashes and tests both the full path
    and a suffix-only match so that absolute paths work correctly on all
    platforms (e.g. C:/tmp/repo/.github/workflows/ci.yml still matches
    the pattern '.github/workflows/**').
    """
    # Normalize OS separators to forward slashes for consistent matching
    norm = path.replace("\\", "/")
    basename = Path(path).name

    for pattern in patterns:
        # Direct match on the normalized full path
        if fnmatch.fnmatch(norm, pattern):
            return True
        # Basename match for simple patterns without directory separators
        if "/" not in pattern and fnmatch.fnmatch(basename, pattern):
            return True
        # Suffix match: check if any trailing portion of the path matches.
        # This handles patterns like ".github/workflows/**" against absolute paths.
        # Strip leading "**/" from pattern for suffix comparison.
        suffix_pattern = pattern.lstrip("*").lstrip("/")
        if suffix_pattern and fnmatch.fnmatch(norm, f"*/{suffix_pattern}"):
            return True
        # Also try matching a sub-path directly (path contains the pattern prefix)
        if suffix_pattern and ("/" + suffix_pattern.rstrip("*").rstrip("/")) in norm:
            # Verify with actual fnmatch on the portion starting with the pattern
            pat_prefix = suffix_pattern.split("*")[0].split("/")[0]
            if pat_prefix:
                idx = norm.find("/" + pat_prefix)
                if idx >= 0:
                    sub = norm[idx + 1 :]
                    if fnmatch.fnmatch(sub, suffix_pattern):
                        return True
    return False


def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy of string *s* in bits."""
    if not s:
        return 0.0
    counts = collections.Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _has_combined_short_flag(token: str, ch: str) -> bool:
    """Return True if *token* is a combined short-option cluster containing *ch*."""
    return bool(re.match(r"^-[A-Za-z]{2,}$", token)) and (ch in token[1:])


def _url_host(url: str) -> str:
    """Extract hostname from a URL string using stdlib urllib.parse."""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _url_path(url: str) -> str:
    """Extract path component from URL (stdlib only, without query/fragment)."""
    parsed = urllib.parse.urlparse(url)
    return parsed.path or "/"


# ---------------------------------------------------------------------------
# Rule evaluators -- shell sub-functions
# ---------------------------------------------------------------------------


def _check_shell_deny_commands(argv0: str) -> PolicyDecision | None:
    """Return DENY if argv0 is a globally blocked command.

    Normalises argv0 with os.path.basename() so that path-based invocations
    like /usr/bin/rm or C:\\Windows\\System32\\cmd.exe are correctly matched
    and receive the expected risk_score_delta=8 (H-1 fix).
    """
    basename = os.path.basename(argv0)
    if basename not in SHELL_DENY_COMMANDS:
        return None
    return PolicyDecision(
        verdict="DENY",
        rule_id="SHELL_DENY_CMD",
        reason=f"Command '{argv0}' is blocked by policy",
        risk_score_delta=8,
    )


def _check_shell_operators(full_cmd: str) -> PolicyDecision | None:
    """Return DENY if any shell pipe/redirect operator is present (NFKC-normalized)."""
    normalized_cmd = unicodedata.normalize("NFKC", full_cmd)
    for op in SHELL_DENY_OPERATORS:
        if op in full_cmd or op in normalized_cmd:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_OPERATOR",
                reason=f"Shell operator '{op}' is blocked by policy",
                risk_score_delta=6,
            )
    return None


def _check_credentials_in_args(argv0: str, argv1: str) -> PolicyDecision | None:
    """Return DENY if a credential sub-command is detected (git credential, gh auth, etc.)."""
    for cmd, blocked_subcmds in SHELL_CREDENTIAL_DENY:
        if argv0 == cmd and argv1 in blocked_subcmds:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_CREDENTIAL_SUBCMD",
                reason=f"Subcommand '{argv0} {argv1}' is blocked (credential access)",
                risk_score_delta=9,
            )
    return None


def _check_python_exec_flags(argv0: str, args: list[str]) -> PolicyDecision | None:
    """Return DENY/REQUIRE_APPROVAL for python/python3 inline-exec and pkg-install patterns."""
    if argv0 not in ("python", "python3"):
        return None

    for token in args[1:]:
        if token == "-c" or _has_combined_short_flag(token, "c"):
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=f"Inline code execution flag '{token}' (contains -c) blocked for '{argv0}'",
                risk_score_delta=9,
            )

    if "-" in args[1:]:
        return PolicyDecision(
            verdict="DENY",
            rule_id="SHELL_DENY_INLINE_EXEC",
            reason=f"Stdin code execution ('-') blocked for '{argv0}'",
            risk_score_delta=9,
        )

    if "-m" in args[1:]:
        m_idx = args[1:].index("-m") + 1  # offset for args[1:] slice
        module = args[m_idx + 1].lower() if m_idx + 1 < len(args) else ""
        if module in _PYTHON_MODULE_PKG_MANAGERS:
            return PolicyDecision(
                verdict="REQUIRE_APPROVAL",
                rule_id="SHELL_PKG_INSTALL",
                reason=(
                    f"Package installation via '{argv0} -m {module}' "
                    "requires approval (supply chain risk)"
                ),
                risk_score_delta=4,
            )

    return None


def _check_pkg_install(
    argv0: str, argv1: str, args: list[str]
) -> PolicyDecision | None:
    """Return REQUIRE_APPROVAL for package installation commands."""
    # DENY: per-command inline code execution flags (cmake, make, go -- defense-in-depth)
    if argv0 in SHELL_DENY_EXEC_FLAGS:
        deny_flags = SHELL_DENY_EXEC_FLAGS[argv0]
        lowered_flags = frozenset(f.lower() for f in deny_flags)
        matched = lowered_flags.intersection(a.lower() for a in args[1:])
        if matched:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=f"Inline code execution flag(s) {sorted(matched)} are blocked for '{argv0}'",
                risk_score_delta=9,
            )

    # DENY: uv run wrapping inline code execution
    if argv0 == "uv" and argv1 == "run":
        matched = _UVR_INLINE_EXEC_FLAGS.intersection(args[2:])
        if matched:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=f"Inline code execution via 'uv run' is blocked (flag(s): {sorted(matched)})",
                risk_score_delta=9,
            )

    # REQUIRE_APPROVAL: standard package managers
    for cmd, install_subcmds in SHELL_PKG_INSTALL_APPROVAL:
        if argv0 == cmd and argv1 in install_subcmds:
            return PolicyDecision(
                verdict="REQUIRE_APPROVAL",
                rule_id="SHELL_PKG_INSTALL",
                reason=f"Package installation '{argv0} {argv1}' requires approval (supply chain risk)",
                risk_score_delta=4,
            )

    # REQUIRE_APPROVAL: uv add / uv pip install
    if argv0 == "uv":
        if argv1 == "add":
            return PolicyDecision(
                verdict="REQUIRE_APPROVAL",
                rule_id="SHELL_PKG_INSTALL",
                reason="Package installation 'uv add' requires approval (supply chain risk)",
                risk_score_delta=4,
            )
        if argv1 == "pip":
            argv2 = args[2].lower() if len(args) > 2 else ""
            if argv2 in _UV_PIP_INSTALL_SUBCMDS:
                return PolicyDecision(
                    verdict="REQUIRE_APPROVAL",
                    rule_id="SHELL_PKG_INSTALL",
                    reason=f"Package installation 'uv pip {argv2}' requires approval (supply chain risk)",
                    risk_score_delta=4,
                )

    return None


# ---------------------------------------------------------------------------
# Rule evaluators
# ---------------------------------------------------------------------------


def _eval_shell(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate shell action rules. Returns decision or None to continue."""
    args = ctx.args
    argv0 = args[0].lower() if args else ""
    argv1 = args[1].lower() if len(args) > 1 else ""
    full_cmd = " ".join(args)

    if decision := _check_shell_deny_commands(argv0):
        return decision

    if decision := _check_shell_operators(full_cmd):
        return decision

    if decision := _check_credentials_in_args(argv0, argv1):
        return decision

    if decision := _check_python_exec_flags(argv0, args):
        return decision

    if decision := _check_pkg_install(argv0, argv1, args):
        return decision

    # REQUIRE_APPROVAL: large file count change
    file_count = ctx.metadata.get("file_count")
    if isinstance(file_count, int) and file_count > FILE_COUNT_APPROVAL_THRESHOLD:
        return PolicyDecision(
            verdict="REQUIRE_APPROVAL",
            rule_id="SHELL_LARGE_FILE_CHANGE",
            reason=f"Shell command affects {file_count} files (>{FILE_COUNT_APPROVAL_THRESHOLD})",
            risk_score_delta=3,
        )

    # ALLOW: safe commands with no pipe/redirect
    if argv0 in SHELL_ALLOW_COMMANDS:
        return PolicyDecision(
            verdict="ALLOW",
            rule_id="SHELL_ALLOW_CMD",
            reason=f"Command '{argv0}' is in the allowlist",
            risk_score_delta=0,
        )

    # Default DENY for unrecognised shell commands
    return PolicyDecision(
        verdict="DENY",
        rule_id="SHELL_DENY_DEFAULT",
        reason=f"Command '{argv0}' is not in the allowlist",
        risk_score_delta=5,
    )


def _eval_file_read(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate file_read action rules."""
    path = ctx.args[0] if ctx.args else ""
    # Resolve symlinks so that e.g. /tmp/link -> /etc/passwd is caught by deny patterns.
    path = os.path.realpath(path) if path else ""

    if _matches_any(path, FILE_READ_DENY_PATTERNS):
        return PolicyDecision(
            verdict="DENY",
            rule_id="FILE_READ_DENY_SENSITIVE",
            reason=f"File path '{path}' matches a sensitive pattern",
            risk_score_delta=7,
        )

    return PolicyDecision(
        verdict="ALLOW",
        rule_id="FILE_READ_ALLOW",
        reason="File read allowed",
        risk_score_delta=0,
    )


def _eval_file_write(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate file_write action rules."""
    path = ctx.args[0] if ctx.args else ""
    # Resolve symlinks so that e.g. /tmp/link -> /etc/passwd is caught by deny patterns.
    path = os.path.realpath(path) if path else ""

    if _matches_any(path, FILE_WRITE_APPROVAL_PATTERNS):
        return PolicyDecision(
            verdict="REQUIRE_APPROVAL",
            rule_id="FILE_WRITE_REQUIRE_APPROVAL",
            reason=f"File path '{path}' requires approval before writing",
            risk_score_delta=4,
        )

    # REQUIRE_APPROVAL: lock file writes indicate dependency changes (G-2)
    if _matches_any(path, FILE_WRITE_LOCKFILE_PATTERNS):
        return PolicyDecision(
            verdict="REQUIRE_APPROVAL",
            rule_id="FILE_WRITE_LOCKFILE",
            reason=f"Lock file '{path}' modification requires approval (supply chain risk)",
            risk_score_delta=4,
        )

    return PolicyDecision(
        verdict="ALLOW",
        rule_id="FILE_WRITE_ALLOW",
        reason="File write allowed",
        risk_score_delta=0,
    )


def _check_host_restrictions(url: str, method: str) -> PolicyDecision | None:
    """Return DENY for mutating methods, over-long URLs, or non-allowlisted hosts."""
    if method in NET_DENY_METHODS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_METHOD",
            reason=f"HTTP method '{method}' is not allowed",
            risk_score_delta=6,
        )
    if len(url) > NET_URL_MAX_LENGTH:
        return PolicyDecision(
            verdict="DENY",
            rule_id="net.url_too_long",
            reason=f"URL length {len(url)} exceeds maximum {NET_URL_MAX_LENGTH}",
            risk_score_delta=8,
        )
    host = _url_host(url)
    if host not in NET_ALLOWLIST_HOSTS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_HOST",
            reason=f"Host '{host}' is not in the allowlist",
            risk_score_delta=5,
        )
    return None


def _check_protocol_rules(url: str, host: str) -> PolicyDecision | None:
    """Return DENY if the URL path is not in the per-host path allowlist."""
    allowed_paths = NET_PATH_ALLOWLIST.get(host)
    if allowed_paths is None:
        return None
    path = _url_path(url)
    if not any(path.startswith(prefix) for prefix in allowed_paths):
        return PolicyDecision(
            verdict="DENY",
            rule_id="net.path_not_allowed",
            reason=f"Path '{path}' is not permitted for host '{host}'",
            risk_score_delta=6,
        )
    return None


def _check_data_exfil(url: str) -> PolicyDecision | None:
    """Return DENY if the URL contains base64, hex, or high-entropy data.

    Checks query string values, query string keys, URL path segments, and
    the userinfo field (user:password@host) to prevent exfiltration through
    allowlisted domains (C-2 fix).
    """
    parsed = urllib.parse.urlparse(url)

    def _check_token(
        token: str, location: str, base64_rule: str, hex_rule: str, entropy_rule: str
    ) -> PolicyDecision | None:
        if _RE_BASE64.match(token):
            return PolicyDecision(
                verdict="DENY",
                rule_id=base64_rule,
                reason=f"URL {location} contains base64-encoded data (potential exfiltration)",
                risk_score_delta=9,
            )
        if _RE_HEX.match(token):
            return PolicyDecision(
                verdict="DENY",
                rule_id=hex_rule,
                reason=f"URL {location} contains hex-encoded data (potential exfiltration)",
                risk_score_delta=9,
            )
        if len(token) > NET_ENTROPY_MIN_LEN:
            entropy = _shannon_entropy(token)
            if entropy > NET_ENTROPY_THRESHOLD:
                return PolicyDecision(
                    verdict="DENY",
                    rule_id=entropy_rule,
                    reason=f"URL {location} has high entropy ({entropy:.2f} bits)",
                    risk_score_delta=9,
                )
        return None

    # Check query string values (use original rule IDs for backward compat)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key, values in qs.items():
        # Check query keys (new in C-2 fix)
        if decision := _check_token(
            key, "query key",
            "net.base64_in_query_key", "net.hex_in_query_key", "net.high_entropy_query_key"
        ):
            return decision
        for value in values:
            # Original rule IDs preserved for backward compat
            if decision := _check_token(
                value, "query value",
                "net.base64_in_query", "net.hex_in_query", "net.high_entropy_query"
            ):
                return decision

    # Check URL path segments (new in C-2 fix)
    for segment in parsed.path.split("/"):
        if segment:
            if decision := _check_token(
                segment, "path segment",
                "net.base64_in_path", "net.hex_in_path", "net.high_entropy_path"
            ):
                return decision

    # Check userinfo (user:password@host) (new in C-2 fix)
    if parsed.username:
        if decision := _check_token(
            parsed.username, "userinfo username",
            "net.base64_in_userinfo", "net.hex_in_userinfo", "net.high_entropy_userinfo"
        ):
            return decision
    if parsed.password:
        if decision := _check_token(
            parsed.password, "userinfo password",
            "net.base64_in_userinfo", "net.hex_in_userinfo", "net.high_entropy_userinfo"
        ):
            return decision

    # Check URL fragment (C-2 fix: fragments may be forwarded by internal
    # code even though browsers don't send them to servers).
    if parsed.fragment:
        if decision := _check_token(
            parsed.fragment, "fragment",
            "net.base64_in_fragment", "net.hex_in_fragment", "net.high_entropy_fragment"
        ):
            return decision

    return None


def _eval_net(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate net action rules."""
    url = ctx.args[0] if ctx.args else ""
    method = ctx.args[1].upper() if len(ctx.args) > 1 else "GET"

    if decision := _check_host_restrictions(url, method):
        return decision

    host = _url_host(url)

    if decision := _check_data_exfil(url):
        return decision

    if decision := _check_protocol_rules(url, host):
        return decision

    return PolicyDecision(
        verdict="ALLOW",
        rule_id="NET_ALLOW",
        reason=f"GET to '{host}' is allowed",
        risk_score_delta=0,
    )


def _eval_git(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate git action rules."""
    # Skip leading global options to find the real subcommand.
    # Git options that consume the next token as a value must be handled
    # specially; otherwise ``git -c key=val push`` would treat ``key=val``
    # as the subcommand and bypass the push deny rule (H-5 fix).
    _GIT_OPTS_WITH_VALUE = frozenset({
        "-c", "-C", "--git-dir", "--work-tree", "--namespace",
        "--config-env", "--super-prefix",
    })
    subcmd = ""
    skip_next = False
    for token in ctx.args:
        if skip_next:
            skip_next = False
            continue
        if token in _GIT_OPTS_WITH_VALUE:
            skip_next = True
            continue
        if token.startswith("-"):
            # Handle --option=value (e.g. --git-dir=/tmp) — single token
            continue
        subcmd = token.lower()
        break

    if subcmd in GIT_DENY_SUBCMDS:
        # DENY unless GIT_PUSH_APPROVAL capability is granted
        from veronica_core.security.capabilities import has_cap

        if not has_cap(ctx.caps, Capability.GIT_PUSH_APPROVAL):
            return PolicyDecision(
                verdict="DENY",
                rule_id="GIT_DENY_SUBCMD",
                reason=f"Git subcommand '{subcmd}' requires GIT_PUSH_APPROVAL capability",
                risk_score_delta=7,
            )

    return PolicyDecision(
        verdict="ALLOW",
        rule_id="GIT_ALLOW",
        reason=f"Git subcommand '{subcmd}' is allowed",
        risk_score_delta=0,
    )


def _eval_browser(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate browser action rules (conservative: deny by default)."""
    return PolicyDecision(
        verdict="DENY",
        rule_id="BROWSER_DENY_DEFAULT",
        reason="Browser actions are not allowed by default policy",
        risk_score_delta=5,
    )
