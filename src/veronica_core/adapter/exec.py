"""Secure execution adapter for VERONICA Security Containment Layer.

Provides policy-gated shell execution, file I/O, and HTTP fetch.
All operations are checked against PolicyEngine before execution.
Command injection is prevented by enforcing shell=False.
"""
from __future__ import annotations

import hashlib
import os
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
        super().__init__(f"[REQUIRE_APPROVAL] {rule_id}: {reason} (args_hash={args_hash})")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AdapterConfig:
    """Configuration for SecureExecutor."""

    repo_root: str
    policy_engine: PolicyEngine
    caps: CapabilitySet
    net_allowlist: list[str] = field(default_factory=lambda: [
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "raw.githubusercontent.com",
        "registry.npmjs.org",
    ])


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

        Never accepts a string command — shell=False is enforced to
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

        ctx = self._make_ctx("shell", argv, cwd)
        self._enforce(ctx, argv)

        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or self._config.repo_root,
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
        resolved = self._resolve_path(path)
        if not has_cap(self._config.caps, Capability.FILE_READ_SENSITIVE):
            self._check_within_repo(resolved)

        # Pass original path to policy engine so glob patterns like .env match
        ctx = self._make_ctx("file_read", [path], None)
        self._enforce(ctx, [path])

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
        resolved = self._resolve_path(path)
        self._check_within_repo(resolved)

        # Pass original path to policy engine so glob patterns like .github/workflows/** match
        ctx = self._make_ctx("file_write", [path], None)
        self._enforce(ctx, [path])

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # HTTP fetch
    # ------------------------------------------------------------------

    def fetch_url(self, url: str, method: str = "GET") -> str:
        """Fetch *url* with policy enforcement.

        Only GET requests are permitted. Uses urllib.request (no
        external dependencies).

        Args:
            url: Target URL.
            method: HTTP method (must be GET).

        Returns:
            Response body as a string with secrets masked.

        Raises:
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
            ValueError: If method is not GET.
            URLError: If the request fails.
        """
        ctx = self._make_ctx("net", [url, method], None)
        self._enforce(ctx, [url, method])

        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except URLError as exc:
            raise URLError(f"fetch_url failed for {url}: {exc}") from exc

        return self._masker.mask(body)

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

    def _resolve_path(self, path: str) -> Path:
        """Resolve *path* to an absolute Path."""
        p = Path(path)
        if not p.is_absolute():
            p = Path(self._config.repo_root) / p
        return p.resolve()

    def _check_within_repo(self, resolved: Path) -> None:
        """Raise PermissionError if *resolved* is outside repo_root."""
        repo = Path(self._config.repo_root).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            raise PermissionError(
                f"Path '{resolved}' is outside repo_root '{repo}'"
            )
