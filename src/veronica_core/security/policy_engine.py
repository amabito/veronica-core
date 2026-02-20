"""Policy Engine for VERONICA Security Containment Layer.

Evaluates PolicyContext against ordered rules and returns PolicyDecision.
Rules are fail-closed: default verdict is DENY.
"""
from __future__ import annotations

import fnmatch
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

SHELL_DENY_OPERATORS: tuple[str, ...] = ("|", ">", ">>", "2>", "&&", ";")

FILE_READ_DENY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/AppData/Local/Google/Chrome/User Data/**",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.kube/**",
)

NET_ALLOWLIST_HOSTS: frozenset[str] = frozenset({
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "raw.githubusercontent.com",
    "registry.npmjs.org",
})

NET_DENY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})

GIT_DENY_SUBCMDS: frozenset[str] = frozenset({"push", "workflow", "release", "tag"})

FILE_WRITE_APPROVAL_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    "package.json",
    ".git/hooks/**",
    "*.ps1",
    "*.bat",
    "*.sh",
)

SHELL_ALLOW_COMMANDS: frozenset[str] = frozenset({
    "pytest", "python", "uv", "npm", "pnpm", "cargo", "go", "make", "cmake",
})

FILE_COUNT_APPROVAL_THRESHOLD = 20


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


def _url_host(url: str) -> str:
    """Extract hostname from a URL string (stdlib only)."""
    # Strip scheme
    rest = url
    if "://" in rest:
        rest = rest.split("://", 1)[1]
    # Strip path
    host = rest.split("/")[0]
    # Strip port
    host = host.split(":")[0]
    return host.lower()


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

    # DENY: GET to non-allowlisted host
    host = _url_host(url)
    if host not in NET_ALLOWLIST_HOSTS:
        return PolicyDecision(
            verdict="DENY",
            rule_id="NET_DENY_HOST",
            reason=f"Host '{host}' is not in the allowlist",
            risk_score_delta=5,
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

    def __init__(self, policy_path: Path | None = None) -> None:
        """Initialize the engine.

        Args:
            policy_path: Optional path to a YAML policy file.
                         Currently the engine uses built-in rules; the path
                         argument is accepted for forward-compatibility.
        """
        # policy_path reserved for future external rule loading
        self._policy_path = policy_path

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
        self.last_decision: PolicyDecision | None = None

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
