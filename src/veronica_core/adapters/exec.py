"""Secure execution adapter for VERONICA Security Containment Layer.

Provides policy-gated shell execution, file I/O, and HTTP fetch.
All operations are checked against PolicyEngine before execution.
Command injection is prevented by enforcing shell=False.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError

from veronica_core.security.capabilities import Capability, CapabilitySet, has_cap
from veronica_core.security.masking import SecretMasker
from veronica_core.security.policy_engine import (
    PolicyContext,
    PolicyEngine,
)

logger = logging.getLogger(__name__)

# Regex for extracting redirect URL from error messages.
# Using a regex avoids fragile string splitting that breaks if the URL
# itself contains the word "blocked".
_RE_REDIRECT_BLOCKED = re.compile(r"Redirect to (.+?) blocked")

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SecurePermissionError(RuntimeError):
    """Raised when PolicyEngine returns DENY for an action."""

    def __init__(self, rule_id: str, reason: str) -> None:
        self.rule_id = rule_id
        self.reason = reason
        super().__init__(f"[DENY] {rule_id}: {reason}")


class ApprovalRequiredError(RuntimeError):
    """Raised when PolicyEngine returns REQUIRE_APPROVAL for an action."""

    def __init__(self, rule_id: str, reason: str, args_hash: str) -> None:
        self.rule_id = rule_id
        self.reason = reason
        self.args_hash = args_hash
        super().__init__(
            f"[REQUIRE_APPROVAL] {rule_id}: {reason} (args_hash={args_hash})"
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AdapterConfig:
    """Configuration for SecureExecutor."""

    repo_root: str
    policy_engine: PolicyEngine
    caps: CapabilitySet
    net_allowlist: list[str] = field(
        default_factory=lambda: [
            "pypi.org",
            "files.pythonhosted.org",
            "github.com",
            "raw.githubusercontent.com",
            "registry.npmjs.org",
        ]
    )


# ---------------------------------------------------------------------------
# SecureExecutor
# ---------------------------------------------------------------------------


class SecureExecutor:
    """Policy-gated executor for shell commands, file I/O, and HTTP fetch.

    All operations are evaluated by PolicyEngine before execution.
    Shell commands are always run with shell=False to prevent injection.
    Sensitive data in outputs is masked via SecretMasker.
    """

    def __init__(self, config: AdapterConfig) -> None:
        self._config = config
        self._masker = SecretMasker()

    # ------------------------------------------------------------------
    # Shell execution
    # ------------------------------------------------------------------

    def execute_shell(
        self,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """Execute *argv* as a subprocess with policy enforcement.

        Never accepts a string command -- shell=False is enforced to
        prevent command injection.

        Args:
            argv: Command and arguments as a list. Must not be empty.
            cwd: Working directory for the subprocess.
            env: Environment variables for the subprocess. If None,
                 inherits the current process environment.
            timeout: Maximum wall-clock seconds before raising TimeoutExpired.

        Returns:
            Tuple of (returncode, stdout, stderr). stdout and stderr are
            masked before being returned.

        Raises:
            ValueError: If argv is empty.
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
            subprocess.TimeoutExpired: If the process exceeds *timeout*.
        """
        if not argv:
            raise ValueError("argv must not be empty")

        # Resolve cwd to an absolute canonical path.  Policy engine receives
        # the resolved cwd so it can enforce directory constraints.
        effective_cwd = cwd or self._config.repo_root
        resolved_cwd = self._resolve_path(effective_cwd)

        ctx = self._make_ctx("shell", argv, str(resolved_cwd))
        self._enforce(ctx, argv)

        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(resolved_cwd),
            env=env,
        )
        stdout = self._masker.mask(result.stdout)
        stderr = self._masker.mask(result.stderr)
        return result.returncode, stdout, stderr

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> str:
        """Read and return the contents of *path*.

        Path traversal outside repo_root is blocked unless the
        FILE_READ_SENSITIVE capability is granted.

        Args:
            path: Absolute or relative file path.

        Returns:
            File contents with secrets masked.

        Raises:
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
            PermissionError: If path escapes repo_root without the capability.
            FileNotFoundError: If the file does not exist.
        """
        abs_path = self._abs_path(path)
        resolved = abs_path.resolve()
        if not has_cap(self._config.caps, Capability.FILE_READ_SENSITIVE):
            self._check_within_repo(resolved)

        # Reject symlinks on the unresolved path.  Path.resolve() follows
        # symlinks, so checking after resolve() would always return False.
        if abs_path.is_symlink():
            raise PermissionError(f"Symlinks are not allowed: {abs_path}")

        # Evaluate policy on BOTH original path (for pattern matching like
        # .env) and the resolved real path (to catch symlink-based bypass).
        ctx = self._make_ctx("file_read", [path], None)
        self._enforce(ctx, [path])
        resolved_str = str(resolved)
        if resolved_str != str(abs_path):
            ctx2 = self._make_ctx("file_read", [resolved_str], None)
            self._enforce(ctx2, [resolved_str])

        content = resolved.read_text(encoding="utf-8", errors="replace")
        return self._masker.mask(content)

    def write_file(self, path: str, content: str) -> None:
        """Write *content* to *path* with policy enforcement.

        Args:
            path: Absolute or relative file path.
            content: Text content to write.

        Raises:
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
            PermissionError: If path escapes repo_root.
        """
        abs_path = self._abs_path(path)
        resolved = abs_path.resolve()
        self._check_within_repo(resolved)

        # Reject symlinks on the unresolved path (before resolve() follows them).
        if abs_path.exists() and abs_path.is_symlink():
            raise PermissionError(f"Symlinks are not allowed: {abs_path}")

        # Evaluate policy on BOTH original path (for pattern matching like
        # .github/workflows/**) and the resolved real path.
        ctx = self._make_ctx("file_write", [path], None)
        self._enforce(ctx, [path])
        resolved_str = str(resolved)
        if resolved_str != str(abs_path):
            ctx2 = self._make_ctx("file_write", [resolved_str], None)
            self._enforce(ctx2, [resolved_str])

        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Re-check the unresolved path for symlink swaps (TOCTOU mitigation).
        if abs_path.is_symlink():
            raise PermissionError(f"Symlinks are not allowed: {abs_path}")
        # Re-resolve after mkdir to verify the target is still within repo.
        final = abs_path.resolve()
        self._check_within_repo(final)
        final.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # HTTP fetch
    # ------------------------------------------------------------------

    _MAX_REDIRECTS = 10

    def fetch_url(self, url: str, method: str = "GET") -> str:
        """Fetch *url* with policy enforcement.

        Only GET requests are permitted. Uses urllib.request (no
        external dependencies). Redirects are followed up to
        ``_MAX_REDIRECTS`` hops, with each target checked against policy.

        Args:
            url: Target URL.
            method: HTTP method (must be GET).

        Returns:
            Response body as a string with secrets masked.

        Raises:
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
            ValueError: If method is not GET.
            URLError: If the request fails or redirect limit exceeded.
        """
        if method.upper() != "GET":
            raise ValueError(f"Only GET requests are permitted, got {method!r}")

        # Use a custom opener that does NOT follow redirects automatically.
        # This prevents outbound connections to non-allowlisted hosts.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(
                self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
            ) -> None:
                raise URLError(
                    f"Redirect to {newurl} blocked (policy requires pre-check)"
                )

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # Disable proxy to prevent egress bypass
            _NoRedirect,
        )
        visited: set[str] = set()
        current = url

        for _ in range(self._MAX_REDIRECTS):
            ctx = self._make_ctx("net", [current, method], None)
            self._enforce(ctx, [current, method])

            req = urllib.request.Request(current, method=method)
            try:
                with opener.open(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                return self._masker.mask(body)
            except URLError as exc:
                err_msg = str(exc.reason) if hasattr(exc, "reason") else str(exc)
                _redirect_match = _RE_REDIRECT_BLOCKED.search(err_msg) if "Redirect to " in err_msg else None
                if _redirect_match:
                    redirect_url = _redirect_match.group(1)
                    if redirect_url in visited:
                        raise URLError(f"Redirect loop detected: {redirect_url}") from exc
                    visited.add(redirect_url)
                    current = redirect_url
                    continue
                logger.debug("[exec] fetch_url failed for %s: %s", current, exc)
                raise URLError("fetch_url failed") from exc

        raise URLError(f"Too many redirects (>{self._MAX_REDIRECTS}) for {url}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_ctx(
        self,
        action: str,
        args: list[str],
        cwd: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> PolicyContext:
        """Build a PolicyContext for the given action."""
        return PolicyContext(
            action=action,  # type: ignore[arg-type]
            args=args,
            working_dir=cwd or self._config.repo_root,
            repo_root=self._config.repo_root,
            user=None,
            caps=self._config.caps,
            env="unknown",
            metadata=metadata or {},
        )

    def _enforce(self, ctx: PolicyContext, args: list[str]) -> None:
        """Evaluate *ctx* and raise if DENY or REQUIRE_APPROVAL."""
        decision = self._config.policy_engine.evaluate(ctx)
        if decision.verdict == "DENY":
            raise SecurePermissionError(decision.rule_id, decision.reason)
        if decision.verdict == "REQUIRE_APPROVAL":
            args_hash = hashlib.sha256(repr(args).encode()).hexdigest()
            raise ApprovalRequiredError(decision.rule_id, decision.reason, args_hash)

    def _abs_path(self, path: str) -> Path:
        """Return *path* as an absolute Path (without resolving symlinks)."""
        p = Path(path)
        if not p.is_absolute():
            p = Path(self._config.repo_root) / p
        return p

    def _resolve_path(self, path: str) -> Path:
        """Resolve *path* to an absolute, symlink-resolved Path."""
        return self._abs_path(path).resolve()

    def _check_within_repo(self, resolved: Path) -> None:
        """Raise PermissionError if *resolved* is outside repo_root."""
        repo = Path(self._config.repo_root).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            raise PermissionError(f"Path '{resolved}' is outside repo_root '{repo}'")
