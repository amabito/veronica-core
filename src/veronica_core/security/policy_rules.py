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
_COMBINED_SHORT_FLAG_RE = re.compile(r"^-[A-Za-z]{2,}$")

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

# Options that consume the next token as a value (used when scanning uv args).
_UV_OPTS_WITH_VALUE: frozenset[str] = frozenset({
    "--project", "--directory", "--config-file", "--cache-dir",
    "--python-preference", "--python-fetch", "--color",
})

# npm/pnpm subcommands that execute arbitrary packages.
_NPM_EXEC_SUBCMDS: frozenset[str] = frozenset({"exec", "dlx", "x"})

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
    return bool(_COMBINED_SHORT_FLAG_RE.match(token)) and (ch in token[1:])


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


def _check_credentials_in_args(argv0: str, argv1: str, args: list[str] | None = None) -> PolicyDecision | None:
    """Return DENY if a credential sub-command is detected (git credential, gh auth, etc.)."""
    for cmd, blocked_subcmds in SHELL_CREDENTIAL_DENY:
        if argv0 != cmd:
            continue
        # Check argv1 directly and also scan non-option tokens for the blocked subcommand
        # to prevent bypass via prepended global options (e.g. npm --silent token list).
        tokens_to_check = {argv1}
        if args:
            tokens_to_check.update(t.lower() for t in args[1:] if not t.startswith("-"))
        matched = tokens_to_check & blocked_subcmds
        if matched:
            found = next(iter(matched))
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_CREDENTIAL_SUBCMD",
                reason=f"Subcommand '{argv0} {found}' is blocked (credential access)",
                risk_score_delta=9,
            )
    return None


def _check_python_exec_flags(argv0: str, args: list[str]) -> PolicyDecision | None:
    """Return DENY/REQUIRE_APPROVAL for python/python3 inline-exec and pkg-install patterns."""
    if argv0 not in ("python", "python3"):
        return None

    for token in args[1:]:
        # Match: -c, -c<code> (attached), -Ic (combined), etc.
        if token == "-c" or token.startswith("-c") or _has_combined_short_flag(token, "c"):
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

    # Detect -m flag including combined short flags like -Im, -mI, -Sm etc.
    for i, arg in enumerate(args[1:], start=1):
        if arg == "-m" or (arg.startswith("-") and not arg.startswith("--") and "m" in arg[1:]):
            # The module name follows the -m flag; for combined flags the
            # module may be embedded in the arg itself (see branches below).
            module_idx = i + 1 if arg == "-m" else i
            # For combined flags where -m is not standalone, module is next arg
            if arg != "-m" and arg[1:].endswith("m"):
                module_idx = i + 1
            elif arg != "-m":
                # -m is not at end: e.g. -mI -> module is embedded, skip
                # -Im -> m at end, module is next arg (handled above)
                # For -mPKG style, the module is the rest after m
                m_pos = arg[1:].index("m")
                rest = arg[2 + m_pos:]
                if rest:
                    # e.g. -mpip -> module is "pip"
                    if rest.lower() in _PYTHON_MODULE_PKG_MANAGERS:
                        return PolicyDecision(
                            verdict="REQUIRE_APPROVAL",
                            rule_id="SHELL_PKG_INSTALL",
                            reason=(
                                f"Package installation via '{argv0} {arg}' "
                                "requires approval (supply chain risk)"
                            ),
                            risk_score_delta=4,
                        )
                    continue
            module = args[module_idx].lower() if module_idx < len(args) else ""
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
            break

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

    # DENY: uv run wrapping inline code execution or wrapped commands.
    # Skip global options (e.g. --project, --no-config) to find "run" subcommand.
    if argv0 == "uv":
        uv_subcmd = ""
        uv_subcmd_idx = -1
        skip_val = False
        for i, tok in enumerate(args[1:], start=1):
            if skip_val:
                skip_val = False
                continue
            if tok in _UV_OPTS_WITH_VALUE:
                skip_val = True
                continue
            if tok.startswith("-"):
                continue
            uv_subcmd = tok.lower()
            uv_subcmd_idx = i
            break

        if uv_subcmd == "run" and uv_subcmd_idx > 0:
            rest = args[uv_subcmd_idx + 1:]
            matched = _UVR_INLINE_EXEC_FLAGS.intersection(rest)
            if matched:
                return PolicyDecision(
                    verdict="DENY",
                    rule_id="SHELL_DENY_INLINE_EXEC",
                    reason=f"Inline code execution via 'uv run' is blocked (flag(s): {sorted(matched)})",
                    risk_score_delta=9,
                )
            # Check wrapped command through the full shell policy pipeline.
            inner = [t for t in rest if t != "--"]
            if inner:
                inner0 = inner[0].lower()
                inner1 = inner[1].lower() if len(inner) > 1 else ""
                inner_cmd = " ".join(inner)
                if decision := _check_shell_deny_commands(inner0):
                    return decision
                if decision := _check_shell_operators(inner_cmd):
                    return decision
                if decision := _check_credentials_in_args(inner0, inner1, inner):
                    return decision
                if decision := _check_python_exec_flags(inner0, inner):
                    return decision
                if decision := _check_pkg_install(inner0, inner1, inner):
                    return decision
                # Inner command must also be in the allowlist.
                if inner0 not in SHELL_ALLOW_COMMANDS:
                    return PolicyDecision(
                        verdict="DENY",
                        rule_id="SHELL_DENY_DEFAULT",
                        reason=f"Wrapped command '{inner0}' (via 'uv run') is not in the allowlist",
                        risk_score_delta=5,
                    )

    # REQUIRE_APPROVAL/DENY: npm/pnpm subcommand detection.
    # Conservative: all --long options (without =) are assumed value-taking
    # to prevent bypass via unknown options (fail-closed).
    # Scan ALL non-option tokens for dangerous subcommands (position-independent).
    # This avoids option-parsing bypass where unknown flags hide the real subcommand.
    if argv0 in ("npm", "pnpm") or argv0 in {cmd for cmd, _ in SHELL_PKG_INSTALL_APPROVAL}:
        non_option_tokens = frozenset(
            t.lower() for t in args[1:] if not t.startswith("-")
        )

        # Check install subcommands
        for cmd, install_subcmds in SHELL_PKG_INSTALL_APPROVAL:
            if argv0 == cmd and (non_option_tokens & install_subcmds):
                matched_sub = next(iter(non_option_tokens & install_subcmds))
                return PolicyDecision(
                    verdict="REQUIRE_APPROVAL",
                    rule_id="SHELL_PKG_INSTALL",
                    reason=f"Package installation '{argv0} {matched_sub}' requires approval (supply chain risk)",
                    risk_score_delta=4,
                )

        if argv0 in ("npm", "pnpm") and (non_option_tokens & _NPM_EXEC_SUBCMDS):
            matched_exec = next(iter(non_option_tokens & _NPM_EXEC_SUBCMDS))
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_PKG_EXEC",
                reason=f"'{argv0} {matched_exec}' can execute arbitrary commands and is blocked",
                risk_score_delta=8,
            )

    # REQUIRE_APPROVAL: uv add / uv pip install.
    # Reuse _UV_OPTS_WITH_VALUE to skip global options for all uv subcommands.
    if argv0 == "uv":
        # Find effective subcommand (skip global options)
        uv_sub = ""
        uv_sub_idx = -1
        skip_v = False
        for i, tok in enumerate(args[1:], start=1):
            if skip_v:
                skip_v = False
                continue
            if tok in _UV_OPTS_WITH_VALUE:
                skip_v = True
                continue
            if tok.startswith("-"):
                continue
            uv_sub = tok.lower()
            uv_sub_idx = i
            break
        if uv_sub == "add":
            return PolicyDecision(
                verdict="REQUIRE_APPROVAL",
                rule_id="SHELL_PKG_INSTALL",
                reason="Package installation 'uv add' requires approval (supply chain risk)",
                risk_score_delta=4,
            )
        if uv_sub == "pip" and uv_sub_idx > 0:
            pip_sub = args[uv_sub_idx + 1].lower() if uv_sub_idx + 1 < len(args) else ""
            if pip_sub in _UV_PIP_INSTALL_SUBCMDS:
                return PolicyDecision(
                    verdict="REQUIRE_APPROVAL",
                    rule_id="SHELL_PKG_INSTALL",
                    reason=f"Package installation 'uv pip {pip_sub}' requires approval (supply chain risk)",
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

    if decision := _check_credentials_in_args(argv0, argv1, args):
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
    """Return DENY for mutating methods, non-HTTPS, over-long URLs, or non-allowlisted hosts."""
    # Enforce HTTPS to protect data integrity and confidentiality.
    if not url.lower().startswith("https://"):
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_SCHEME",
            reason=f"Only HTTPS URLs are permitted, got: {url[:60]}",
            risk_score_delta=7,
        )
    if method != "GET":
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_METHOD",
            reason=f"HTTP method '{method}' is not allowed (only GET permitted)",
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
    # Handle both '&' and ';' as query separators (RFC 2396 legacy).
    # Replace ';' with '&' before parsing because parse_qs separator param
    # matches the full string, not individual characters.
    qs = urllib.parse.parse_qs(
        parsed.query.replace(";", "&"), keep_blank_values=True
    )
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

    # Check URL path segments and matrix parameters (;key=value).
    # URL-decode each segment individually so that encoded slashes (%2F)
    # do not alter segment boundaries and evade length/entropy thresholds.
    # urlparse only extracts params from the LAST segment, so we also
    # split every segment on ';' and '=' to catch matrix params on all segments.
    # Re-attach parsed.params to the path so the last segment's params are included.
    _full_path = parsed.path
    if parsed.params:
        _full_path += ";" + parsed.params
    for raw_segment in _full_path.split("/"):
        # Split segment on ';' to extract matrix params, then '=' for key/value.
        for raw_part in raw_segment.split(";"):
            for raw_token in raw_part.split("="):
                token = urllib.parse.unquote(raw_token)
                if token:
                    if decision := _check_token(
                        token, "path segment",
                        "net.base64_in_path", "net.hex_in_path", "net.high_entropy_path"
                    ):
                        return decision

    # Check userinfo (user:password@host) (new in C-2 fix)
    # Explicitly unquote in case urlparse preserves percent-encoded chars.
    if parsed.username:
        if decision := _check_token(
            urllib.parse.unquote(parsed.username), "userinfo username",
            "net.base64_in_userinfo", "net.hex_in_userinfo", "net.high_entropy_userinfo"
        ):
            return decision
    if parsed.password:
        if decision := _check_token(
            urllib.parse.unquote(parsed.password), "userinfo password",
            "net.base64_in_userinfo", "net.hex_in_userinfo", "net.high_entropy_userinfo"
        ):
            return decision

    # Check URL fragment (C-2 fix: fragments may be forwarded by internal
    # code even though browsers don't send them to servers).
    if parsed.fragment:
        if decision := _check_token(
            urllib.parse.unquote(parsed.fragment), "fragment",
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
        "--config-env", "--super-prefix", "--exec-path",
    })
    subcmd = ""
    skip_next = False
    # Strip leading executable name (e.g. "git", "git.exe", "/usr/bin/git")
    git_args = ctx.args
    if git_args and os.path.basename(git_args[0]).lower().rstrip(".exe") == "git":
        git_args = git_args[1:]
    for token in git_args:
        if skip_next:
            skip_next = False
            continue
        if token in _GIT_OPTS_WITH_VALUE:
            skip_next = True
            continue
        if token.startswith("-"):
            # Handle --option=value (e.g. --git-dir=/tmp) -- single token
            continue
        subcmd = token.lower()
        break

    # Also scan all non-option tokens for denied subcommands (defense-in-depth
    # against option-parsing gaps that could hide a blocked subcommand).
    denied_anywhere = {t.lower() for t in git_args if not t.startswith("-")} & GIT_DENY_SUBCMDS
    if subcmd in GIT_DENY_SUBCMDS or denied_anywhere:
        matched = subcmd if subcmd in GIT_DENY_SUBCMDS else next(iter(denied_anywhere))
        # DENY unless GIT_PUSH_APPROVAL capability is granted
        from veronica_core.security.capabilities import has_cap

        if not has_cap(ctx.caps, Capability.GIT_PUSH_APPROVAL):
            return PolicyDecision(
                verdict="DENY",
                rule_id="GIT_DENY_SUBCMD",
                reason=f"Git subcommand '{matched}' requires GIT_PUSH_APPROVAL capability",
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
