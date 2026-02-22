"""Policy Engine for VERONICA Security Containment Layer.

Evaluates PolicyContext against ordered rules and returns PolicyDecision.
Rules are fail-closed: default verdict is DENY.
"""
from __future__ import annotations

import collections
import fnmatch
import math
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from veronica_core.security.capabilities import Capability, CapabilitySet
from veronica_core.shield.types import Decision, ToolCallContext

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

ActionLiteral = Literal["shell", "file_read", "file_write", "net", "git", "browser"]

SHELL_DENY_COMMANDS: frozenset[str] = frozenset({
    "rm", "del", "format", "reg", "schtasks", "wmic",
    "powershell", "cmd", "certutil", "bitsadmin",
    "curl", "wget", "scp", "sftp",
})

SHELL_DENY_OPERATORS: tuple[str, ...] = (
    "|", ">", ">>", "2>", "&&", ";",
    # Command substitution: $(...) and backtick forms allow arbitrary sub-shell execution
    # even in arguments passed to allowlisted commands (e.g. "echo $(cat /etc/passwd)").
    "$(", "`",
    # Newline injection: allows multi-command payloads embedded in a single argument string.
    "\n",
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
)

NET_ALLOWLIST_HOSTS: frozenset[str] = frozenset({
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "raw.githubusercontent.com",
    "registry.npmjs.org",
})

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

SHELL_ALLOW_COMMANDS: frozenset[str] = frozenset({
    "pytest", "python", "uv", "npm", "pnpm", "cargo", "go", "cmake",
    # "make" removed in v0.10.3 (R-3): Makefile recipes spawn sub-shells that are
    # invisible to PolicyEngine, enabling arbitrary code execution via a crafted
    # Makefile even without dangerous flags.  If build automation is needed, run
    # make inside an OS-level sandbox (Docker, gVisor) outside veronica-core's scope.
})

# Per-command inline code execution flags (defense-in-depth).
# When argv0 matches a key AND any of the associated flags appear in args[1:], DENY
# with risk_score_delta=9 — regardless of SHELL_ALLOW_COMMANDS.
#
# NOTE: python/python3 are intentionally absent here.  They are handled by a
# dedicated combined-flag-aware block in _eval_shell (R-1 fix, v0.10.3) that
# additionally catches combined short-option clusters such as "-Sc" and "-cS".
#
#   cmake -P /tmp/evil.cmake   → executes arbitrary CMake script
#   cmake -E <cmd>             → CMake command-line tool mode
#   make  --eval "<expr>"      → evaluates arbitrary make expression
#   make  -f /tmp/evil.mk      → loads an arbitrary Makefile (defense-in-depth;
#                                make is also removed from SHELL_ALLOW_COMMANDS)
SHELL_DENY_EXEC_FLAGS: dict[str, frozenset[str]] = {
    "cmake":   frozenset({"-P", "-E"}),
    "make":    frozenset({"--eval", "-f"}),
}

# Inline-execution flags scanned inside "uv run <cmd> ..." wrappers.
# "uv run python -c '...'" passes argv0=uv but the inner python still executes
# inline code; catch it by scanning all args[2:] for these flags.
_UVR_INLINE_EXEC_FLAGS: frozenset[str] = frozenset({"-c", "--eval"})

# Python modules that invoke package-manager operations when used with -m.
# "python -m pip install X" bypasses SHELL_PKG_INSTALL (which checks argv0=="pip")
# because argv0 is "python".  This set is used in _eval_shell to gate such calls
# under REQUIRE_APPROVAL (R-2 fix, v0.10.3).
_PYTHON_MODULE_PKG_MANAGERS: frozenset[str] = frozenset({"pip", "pip3", "ensurepip"})

FILE_COUNT_APPROVAL_THRESHOLD = 20

# Supply chain guard (G-2): package install subcommands requiring approval.
# Maps argv0 → subcommands that trigger REQUIRE_APPROVAL.
SHELL_PKG_INSTALL_APPROVAL: tuple[tuple[str, frozenset[str]], ...] = (
    ("pip",    frozenset({"install", "download"})),
    ("pip3",   frozenset({"install", "download"})),
    ("npm",    frozenset({"install", "add", "i"})),
    ("pnpm",   frozenset({"install", "add", "i"})),
    ("yarn",   frozenset({"install", "add"})),
    ("cargo",  frozenset({"add", "install"})),
)

# uv sub-commands that indicate package installation
_UV_INSTALL_SUBCMDS: frozenset[str] = frozenset({"add", "pip"})
# uv pip sub-subcommands that indicate installation
_UV_PIP_INSTALL_SUBCMDS: frozenset[str] = frozenset({"install", "download"})

# Lock file path patterns that require approval on write (G-2)
FILE_WRITE_LOCKFILE_PATTERNS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
    "requirements.txt",
    "requirements-*.txt",
)

# Credential sub-command deny rules (E-2)
# Each entry: (argv0, blocked_subcommands_set)
# Evaluated after SHELL_DENY_COMMANDS and before SHELL_ALLOW_COMMANDS.
SHELL_CREDENTIAL_DENY: tuple[tuple[str, frozenset[str]], ...] = (
    ("git",  frozenset({"credential", "credentials"})),
    ("gh",   frozenset({"auth", "token", "secret"})),
    ("npm",  frozenset({"token", "login", "logout", "adduser", "set-script"})),
    ("pip",  frozenset({"config"})),
)


@dataclass(frozen=True)
class PolicyContext:
    """Immutable snapshot describing an action to be evaluated."""

    action: ActionLiteral
    args: list[str]
    working_dir: str
    repo_root: str
    user: str | None
    caps: CapabilitySet
    env: str  # "dev" | "ci" | "audit" | "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a policy evaluation."""

    verdict: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    rule_id: str
    reason: str
    risk_score_delta: int  # 0=safe, 1-5=moderate, 6-10=critical


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
                    sub = norm[idx + 1:]
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
    """Return True if *token* is a combined short-option cluster containing *ch*.

    Matches tokens of the form ``-[A-Za-z]{2,}`` (a hyphen followed by two or
    more ASCII letters).  Single-character tokens like ``-c`` are intentionally
    *not* matched here; callers must handle them with a plain ``== "-c"`` check.

    Examples::

        _has_combined_short_flag("-Sc", "c")   # True  — python -Sc "code"
        _has_combined_short_flag("-cS", "c")   # True  — python -cS "code"
        _has_combined_short_flag("-ISc", "c")  # True  — python -ISc "code"
        _has_combined_short_flag("-S", "c")    # False — only one letter
        _has_combined_short_flag("--foo", "c") # False — long option
        _has_combined_short_flag("-Wdefault", "c") # False — 'c' not present

    Note: tokens like ``-Wc`` (where ``c`` is a value for ``-W``, not ``-c``)
    will return True.  This is intentionally conservative — prefer DENY over
    ALLOW when the exact semantics are ambiguous.
    """
    return bool(re.match(r"^-[A-Za-z]{2,}$", token)) and (ch in token[1:])


def _url_host(url: str) -> str:
    """Extract hostname from a URL string using stdlib urllib.parse.

    Uses the same parser as _url_path() to prevent host/path inconsistencies
    that could allow an adversary to craft a URL that passes the host allowlist
    check but routes to a different host at the path-check stage.
    """
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _url_path(url: str) -> str:
    """Extract path component from URL (stdlib only, without query/fragment)."""
    parsed = urllib.parse.urlparse(url)
    return parsed.path or "/"


# ---------------------------------------------------------------------------
# Rule evaluators
# ---------------------------------------------------------------------------

def _eval_shell(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate shell action rules. Returns decision or None to continue."""
    args = ctx.args
    argv0 = args[0].lower() if args else ""
    full_cmd = " ".join(args)

    # DENY: dangerous commands
    if argv0 in SHELL_DENY_COMMANDS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="SHELL_DENY_CMD",
            reason=f"Command '{argv0}' is blocked by policy",
            risk_score_delta=8,
        )

    # DENY: pipe or redirect operators in any argument
    for op in SHELL_DENY_OPERATORS:
        if op in full_cmd:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_OPERATOR",
                reason=f"Shell operator '{op}' is blocked by policy",
                risk_score_delta=6,
            )

    # DENY: credential sub-commands (git credential, gh auth, npm token, pip config, etc.)
    argv1 = args[1].lower() if len(args) > 1 else ""
    for cmd, blocked_subcmds in SHELL_CREDENTIAL_DENY:
        if argv0 == cmd and argv1 in blocked_subcmds:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_CREDENTIAL_SUBCMD",
                reason=f"Subcommand '{argv0} {argv1}' is blocked (credential access)",
                risk_score_delta=9,
            )

    # DENY / GATE: python/python3 — combined-flag-aware inline exec, stdin
    # execution, and module-based package installation (R-1 and R-2, v0.10.3).
    #
    # This block replaces the old SHELL_DENY_EXEC_FLAGS["python"] entry (which
    # only matched exact "-c") to also catch combined short-option clusters such
    # as "-Sc", "-cS", and "-ISc" that Python's own argument parser expands into
    # individual flags.
    if argv0 in ("python", "python3"):
        # 1. Inline code execution via -c (exact token or combined cluster).
        for token in args[1:]:
            if token == "-c" or _has_combined_short_flag(token, "c"):
                return PolicyDecision(
                    verdict="DENY",
                    rule_id="SHELL_DENY_INLINE_EXEC",
                    reason=(
                        f"Inline code execution flag '{token}' "
                        f"(contains -c) blocked for '{argv0}'"
                    ),
                    risk_score_delta=9,
                )

        # 2. Stdin code execution: 'python -' reads and executes code from stdin.
        if "-" in args[1:]:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=f"Stdin code execution ('-') blocked for '{argv0}'",
                risk_score_delta=9,
            )

        # 3. Package installation via -m (e.g. 'python -m pip install X').
        #    argv0 is "python" so SHELL_PKG_INSTALL (which checks argv0=="pip")
        #    would not fire; gate it here under the same REQUIRE_APPROVAL verdict.
        if "-m" in args[1:]:
            m_idx = args.index("-m")
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

    # DENY: per-command inline code execution flags (cmake, make — defense-in-depth).
    # Must run before SHELL_ALLOW_COMMANDS so that allowlisted commands cannot
    # execute arbitrary code without a file on disk.
    if argv0 in SHELL_DENY_EXEC_FLAGS:
        deny_flags = SHELL_DENY_EXEC_FLAGS[argv0]
        matched = deny_flags.intersection(args[1:])
        if matched:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=(
                    f"Inline code execution flag(s) {sorted(matched)} "
                    f"are blocked for '{argv0}'"
                ),
                risk_score_delta=9,
            )

    # DENY: uv run wrapping inline code execution (e.g. "uv run python -c '...'").
    # argv0=uv is allowlisted but the inner python still executes inline code.
    if argv0 == "uv" and argv1 == "run":
        matched = _UVR_INLINE_EXEC_FLAGS.intersection(args[2:])
        if matched:
            return PolicyDecision(
                verdict="DENY",
                rule_id="SHELL_DENY_INLINE_EXEC",
                reason=(
                    f"Inline code execution via 'uv run' is blocked "
                    f"(flag(s): {sorted(matched)})"
                ),
                risk_score_delta=9,
            )

    # REQUIRE_APPROVAL: package installation (G-2 supply chain guard)
    # Standard package managers: pip/pip3/npm/pnpm/yarn/cargo
    for cmd, install_subcmds in SHELL_PKG_INSTALL_APPROVAL:
        if argv0 == cmd and argv1 in install_subcmds:
            return PolicyDecision(
                verdict="REQUIRE_APPROVAL",
                rule_id="SHELL_PKG_INSTALL",
                reason=f"Package installation '{argv0} {argv1}' requires approval (supply chain risk)",
                risk_score_delta=4,
            )
    # uv add → install; uv pip install → install
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


def _eval_net(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate net action rules."""
    url = ctx.args[0] if ctx.args else ""
    method = ctx.args[1].upper() if len(ctx.args) > 1 else "GET"

    # DENY: mutating HTTP methods
    if method in NET_DENY_METHODS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_METHOD",
            reason=f"HTTP method '{method}' is not allowed",
            risk_score_delta=6,
        )

    # DENY: URL length exceeds limit
    if len(url) > NET_URL_MAX_LENGTH:
        return PolicyDecision(
            verdict="DENY",
            rule_id="net.url_too_long",
            reason=f"URL length {len(url)} exceeds maximum {NET_URL_MAX_LENGTH}",
            risk_score_delta=8,
        )

    # DENY: GET to non-allowlisted host
    host = _url_host(url)
    if host not in NET_ALLOWLIST_HOSTS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_HOST",
            reason=f"Host '{host}' is not in the allowlist",
            risk_score_delta=5,
        )

    # For GET requests: inspect query string for exfiltration indicators
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for values in qs.values():
        for value in values:
            # Base64-encoded data check
            if _RE_BASE64.match(value):
                return PolicyDecision(
                    verdict="DENY",
                    rule_id="net.base64_in_query",
                    reason="Query string contains base64-encoded data (potential exfiltration)",
                    risk_score_delta=9,
                )
            # Hex string check
            if _RE_HEX.match(value):
                return PolicyDecision(
                    verdict="DENY",
                    rule_id="net.hex_in_query",
                    reason="Query string contains hex-encoded data (potential exfiltration)",
                    risk_score_delta=9,
                )
            # High-entropy value check
            if len(value) > NET_ENTROPY_MIN_LEN and _shannon_entropy(value) > NET_ENTROPY_THRESHOLD:
                return PolicyDecision(
                    verdict="DENY",
                    rule_id="net.high_entropy_query",
                    reason=f"Query string value has high entropy ({_shannon_entropy(value):.2f} bits)",
                    risk_score_delta=9,
                )

    # DENY: path not in per-host allowlist
    allowed_paths = NET_PATH_ALLOWLIST.get(host)
    if allowed_paths is not None:
        path = _url_path(url)
        if not any(path.startswith(prefix) for prefix in allowed_paths):
            return PolicyDecision(
                verdict="DENY",
                rule_id="net.path_not_allowed",
                reason=f"Path '{path}' is not permitted for host '{host}'",
                risk_score_delta=6,
            )

    return PolicyDecision(
        verdict="ALLOW",
        rule_id="NET_ALLOW",
        reason=f"GET to '{host}' is allowed",
        risk_score_delta=0,
    )


def _eval_git(ctx: PolicyContext) -> PolicyDecision | None:
    """Evaluate git action rules."""
    subcmd = ctx.args[0].lower() if ctx.args else ""

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


# ---------------------------------------------------------------------------
# Policy Engine
# ---------------------------------------------------------------------------

_EVALUATORS = {
    "shell": _eval_shell,
    "file_read": _eval_file_read,
    "file_write": _eval_file_write,
    "net": _eval_net,
    "git": _eval_git,
    "browser": _eval_browser,
}

_DEFAULT_DENY = PolicyDecision(
    verdict="DENY",
    rule_id="DEFAULT_DENY",
    reason="No rule matched; fail-closed policy applies",
    risk_score_delta=5,
)


class PolicyEngine:
    """Evaluates PolicyContext against security rules and returns PolicyDecision.

    Rules are evaluated in order: DENY first, then REQUIRE_APPROVAL, then ALLOW.
    Unknown actions are denied by default (fail-closed).
    """

    def __init__(
        self,
        policy_path: Path | None = None,
        public_key_path: Path | None = None,
    ) -> None:
        """Initialize the engine.

        If *policy_path* is provided, its signature is verified.
        v2 (ed25519) is checked first via ``.sig.v2``; v1 (HMAC-SHA256)
        via ``.sig`` is the fallback.  A mismatch raises ``RuntimeError``.
        A missing sig file logs a ``policy_sig_missing`` warning and
        allows the engine to continue loading (backward compatibility).

        In CI or PROD security levels:
        - A missing signature file raises ``RuntimeError`` (not just a warning).
        - If ed25519 (cryptography package) is unavailable, raises ``RuntimeError``.

        Args:
            policy_path: Optional path to a YAML policy file.
            public_key_path: Optional path to the ed25519 public key PEM
                             used for v2 verification.  Defaults to
                             ``policies/public_key.pem`` in the repo root.
        """
        from veronica_core.security.security_level import SecurityLevel, get_security_level
        from veronica_core.security.policy_signing import _ED25519_AVAILABLE

        self._policy_path = policy_path
        self._public_key_path = public_key_path
        self._policy: dict[str, Any] = {}
        self._audit_log = None

        level = get_security_level()
        strict = level in (SecurityLevel.CI, SecurityLevel.PROD)

        if strict and not _ED25519_AVAILABLE:
            raise RuntimeError(
                f"cryptography package is required in {level.name} environment. "
                "Install it with: pip install cryptography"
            )

        if policy_path is not None:
            if strict:
                sig_v2_path = Path(str(policy_path) + ".sig.v2")
                sig_v1_path = Path(str(policy_path) + ".sig")
                if not sig_v2_path.exists() and not sig_v1_path.exists():
                    raise RuntimeError(
                        f"Policy signature file missing in {level.name} environment: "
                        f"{policy_path}"
                    )
            self._verify_policy_signature(policy_path, public_key_path=public_key_path)
            self._policy = self._load_policy(policy_path)
            self._check_rollback()

    # ------------------------------------------------------------------
    # Signature verification (G-1)
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_policy_signature(
        policy_path: Path,
        public_key_path: Path | None = None,
    ) -> None:
        """Verify policy signature for *policy_path*.

        Checks v2 (ed25519) first if a ``.sig.v2`` file exists, then falls back
        to v1 (HMAC-SHA256) via ``.sig``.  Raises RuntimeError on tamper;
        logs warning if no signature file is found.

        Args:
            policy_path: Path to the YAML policy file.
            public_key_path: Optional path to the ed25519 public key PEM.
        """
        import logging as _logging

        from veronica_core.security.policy_signing import PolicySigner, PolicySignerV2

        _log = _logging.getLogger(__name__)

        sig_v2_path = Path(str(policy_path) + ".sig.v2")
        sig_v1_path = Path(str(policy_path) + ".sig")

        def _emit_audit(event: str, payload: dict) -> None:
            try:
                from veronica_core.audit.log import AuditLog
                import tempfile
                audit_dir = Path(tempfile.gettempdir()) / "veronica_audit"
                audit_log = AuditLog(audit_dir / "policy.jsonl")
                audit_log.write(event, payload)
            except Exception:
                pass

        # ------------------------------------------------------------------
        # v2: ed25519
        # ------------------------------------------------------------------
        if sig_v2_path.exists():
            signer_v2 = PolicySignerV2(public_key_path=public_key_path)
            if not signer_v2.verify(policy_path, sig_v2_path):
                _emit_audit("policy_tamper", {"policy_path": str(policy_path), "version": "v2"})
                _log.error("policy_tamper (v2): signature mismatch for %s", policy_path)
                raise RuntimeError(f"Policy tamper detected (v2): {policy_path}")
            # v2 verified — enforce key pin before continuing
            resolved_pub = public_key_path or signer_v2._public_key_path
            if resolved_pub.exists():
                try:
                    from veronica_core.security.key_pin import KeyPinChecker
                    pub_pem = resolved_pub.read_bytes()
                    audit_log = None
                    try:
                        from veronica_core.audit.log import AuditLog
                        import tempfile
                        audit_dir = Path(tempfile.gettempdir()) / "veronica_audit"
                        audit_log = AuditLog(audit_dir / "policy.jsonl")
                    except Exception:
                        pass
                    KeyPinChecker(audit_log).enforce(pub_pem)
                except Exception as exc:
                    if isinstance(exc, RuntimeError):
                        raise
                    _log.warning("key_pin check failed unexpectedly: %s", exc)
            # skip v1 check
            return

        # ------------------------------------------------------------------
        # v1: HMAC-SHA256
        # ------------------------------------------------------------------
        if sig_v1_path.exists():
            signer = PolicySigner()
            if not signer.verify(policy_path, sig_v1_path):
                try:
                    actual = sig_v1_path.read_text(encoding="utf-8").strip()
                except OSError:
                    actual = "<unreadable>"
                expected = signer.sign(policy_path)
                _emit_audit(
                    "policy_tamper",
                    {"policy_path": str(policy_path), "expected": expected, "actual": actual},
                )
                _log.error("policy_tamper: signature mismatch for %s", policy_path)
                raise RuntimeError(f"Policy tamper detected: {policy_path}")
            return

        # ------------------------------------------------------------------
        # No signature file found
        # ------------------------------------------------------------------
        _emit_audit("policy_sig_missing", {"policy_path": str(policy_path)})
        _log.warning("policy_sig_missing: no signature file found for %s", policy_path)

    @staticmethod
    def _load_policy(policy_path: Path) -> dict[str, Any]:
        """Load and parse a YAML policy file.

        Behaviour (v0.10.3 fail-closed change, R-5):

        * If the file **does not exist**: emit a warning and return ``{}``.
          This preserves backward compatibility for callers that optionally
          supply a policy path that may not yet be present.

        * If the file **exists** but cannot be loaded (pyyaml missing, YAML
          parse error, permission denied, encoding error, etc.):
          **raise RuntimeError** (fail-closed).  Silently ignoring a corrupt
          or truncated policy file would skip rollback checks and drop all
          YAML-defined rules, which is worse than an explicit startup failure.

        Args:
            policy_path: Path to the YAML policy file.

        Returns:
            Parsed policy dict, or ``{}`` when the file is absent.

        Raises:
            RuntimeError: If the file exists but pyyaml is unavailable or
                parsing fails.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)

        # File absent — warn and return empty dict (backward-compatible path).
        if not policy_path.exists():
            _log.warning(
                "policy_load_failed: policy file not found: %s",
                policy_path,
            )
            return {}

        # File present — any failure from this point is fail-closed.
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                f"policy_load_failed: pyyaml is required to load the policy "
                f"at {policy_path}. Install it with: pip install pyyaml"
            ) from exc

        try:
            with policy_path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception as exc:
            raise RuntimeError(
                f"policy_load_failed: policy file exists but could not be "
                f"parsed: {policy_path} ({type(exc).__name__})"
            ) from exc

    def _check_rollback(self) -> None:
        """Check policy_version / min_engine_version fields if present.

        Delegates to RollbackGuard.  If audit_log is None, the guard still
        validates engine version but skips persistent rollback tracking.
        """
        from veronica_core.security.rollback_guard import RollbackGuard

        policy_version = self._policy.get("policy_version")
        min_engine = self._policy.get("min_engine_version")
        if policy_version is not None:
            guard = RollbackGuard(audit_log=self._audit_log)
            guard.check(int(policy_version), min_engine)

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Evaluate *ctx* and return a PolicyDecision.

        The engine delegates to a per-action evaluator function.
        If the action is unknown, DENY is returned immediately.
        """
        evaluator = _EVALUATORS.get(ctx.action)
        if evaluator is None:
            return PolicyDecision(
                verdict="DENY",
                rule_id="UNKNOWN_ACTION",
                reason=f"Action '{ctx.action}' is not recognised",
                risk_score_delta=5,
            )

        result = evaluator(ctx)
        return result if result is not None else _DEFAULT_DENY


# ---------------------------------------------------------------------------
# PolicyHook — ToolDispatchHook + EgressBoundaryHook integration
# ---------------------------------------------------------------------------

class PolicyHook:
    """Implements ToolDispatchHook and EgressBoundaryHook protocols.

    Wraps PolicyEngine to intercept tool calls and egress requests.

    Attributes:
        last_decision: The most recent PolicyDecision evaluated.
    """

    def __init__(
        self,
        engine: PolicyEngine | None = None,
        caps: CapabilitySet | None = None,
        working_dir: str = ".",
        repo_root: str = ".",
        env: str = "unknown",
    ) -> None:
        self._engine = engine or PolicyEngine()
        self._caps = caps or CapabilitySet.dev()
        self._working_dir = working_dir
        self._repo_root = repo_root
        self._env = env
        self._last_decision: PolicyDecision | None = None
        self._last_decision_lock = threading.Lock()

    @property
    def last_decision(self) -> PolicyDecision | None:
        """The most recent PolicyDecision evaluated (thread-safe read)."""
        with self._last_decision_lock:
            return self._last_decision

    @last_decision.setter
    def last_decision(self, value: PolicyDecision | None) -> None:
        with self._last_decision_lock:
            self._last_decision = value

    def _verdict_to_decision(self, verdict: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]) -> Decision:
        if verdict == "ALLOW":
            return Decision.ALLOW
        if verdict == "REQUIRE_APPROVAL":
            return Decision.QUARANTINE
        return Decision.HALT

    def before_tool_call(self, ctx: ToolCallContext) -> Decision | None:
        """Intercept tool dispatch. Extract action from ctx.metadata."""
        meta = ctx.metadata or {}
        action: str = meta.get("action", "shell")
        args: list[str] = meta.get("args", [])

        policy_ctx = PolicyContext(
            action=action,  # type: ignore[arg-type]
            args=args,
            working_dir=meta.get("working_dir", self._working_dir),
            repo_root=meta.get("repo_root", self._repo_root),
            user=ctx.user_id,
            caps=self._caps,
            env=self._env,
            metadata=meta,
        )
        decision = self._engine.evaluate(policy_ctx)
        self.last_decision = decision
        return self._verdict_to_decision(decision.verdict)

    def before_egress(
        self, ctx: ToolCallContext, url: str, method: str
    ) -> Decision | None:
        """Intercept outbound HTTP request."""
        policy_ctx = PolicyContext(
            action="net",
            args=[url, method],
            working_dir=self._working_dir,
            repo_root=self._repo_root,
            user=ctx.user_id,
            caps=self._caps,
            env=self._env,
            metadata=ctx.metadata or {},
        )
        decision = self._engine.evaluate(policy_ctx)
        self.last_decision = decision
        return self._verdict_to_decision(decision.verdict)
