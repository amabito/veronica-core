"""Windows Sandbox hardening for VERONICA Security Containment Layer.

Prevents sandbox processes from reading host user profile paths by
intercepting and validating all file path arguments before execution.

No external dependencies required — stdlib only.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.adapter.exec import SecureExecutor


# ---------------------------------------------------------------------------
# Default blocked path prefixes
# ---------------------------------------------------------------------------

def _default_blocked_paths() -> list[str]:
    """Return default list of paths that should be blocked inside the sandbox.

    Includes common Windows user profile locations.  Values are read from the
    process environment at call-time so that tests can override them.
    """
    blocked: list[str] = [
        r"C:\Users",
        "C:/Users",
        r"C:\Windows\System32",
        "C:/Windows/System32",
    ]

    appdata = os.environ.get("APPDATA")
    if appdata:
        blocked.append(appdata)

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        blocked.append(userprofile)

    return blocked


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class WindowsSandboxConfig:
    """Configuration for WindowsSandboxRunner.

    Args:
        repo_root: Absolute path to the original repository root.
        ephemeral_dir: If provided, use this directory instead of creating a
            new temp directory.  The caller is responsible for cleanup.
        blocked_paths: Path prefixes that sandbox processes must not reference.
            Defaults to common Windows user-profile locations.
    """

    repo_root: str
    ephemeral_dir: str | None = None
    blocked_paths: list[str] = field(default_factory=_default_blocked_paths)


# ---------------------------------------------------------------------------
# WindowsSandboxRunner
# ---------------------------------------------------------------------------


class WindowsSandboxRunner:
    """Context manager providing a path-restricted ephemeral sandbox.

    Before each command is executed, every string element of *argv* is
    inspected.  If any element looks like a path pointing into a blocked
    prefix, a :class:`PermissionError` is raised and the command is not
    executed.

    On entry:
        - A temporary directory is created (or *ephemeral_dir* is used).
        - A copy of *repo_root* is placed inside the temp directory.

    On exit:
        - The temporary directory is deleted if it was created internally.

    Usage::

        cfg = WindowsSandboxConfig(repo_root="/path/to/repo")
        with WindowsSandboxRunner(cfg, executor) as runner:
            rc, out, err = runner.run_in_sandbox(["pytest", "tests/"])

    Args:
        config: WindowsSandboxConfig controlling sandbox behaviour.
        executor: SecureExecutor used to run commands inside the sandbox.
    """

    def __init__(
        self,
        config: WindowsSandboxConfig,
        executor: "SecureExecutor",
    ) -> None:
        self._config = config
        self._executor = executor
        self._temp_dir: str | None = None
        self._owns_temp_dir: bool = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "WindowsSandboxRunner":
        self._setup()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._teardown()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def sandbox_dir(self) -> str:
        """Absolute path to the ephemeral working directory."""
        if self._temp_dir is None:
            raise RuntimeError(
                "WindowsSandboxRunner is not active; use it as a context manager"
            )
        return self._temp_dir

    def read_path_blocked(self, path: str) -> bool:
        """Return True if *path* resolves under any blocked prefix.

        Comparison is case-insensitive and handles both forward and
        backslash separators.

        Args:
            path: File path string to check.

        Returns:
            True if the path matches a blocked prefix, False otherwise.
        """
        # Normalise to forward slashes and lowercase for comparison
        norm_path = path.replace("\\", "/").lower()

        for blocked in self._config.blocked_paths:
            norm_blocked = blocked.replace("\\", "/").lower().rstrip("/")
            if norm_path.startswith(norm_blocked + "/") or norm_path == norm_blocked:
                return True

        return False

    def run_in_sandbox(
        self,
        argv: list[str],
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """Execute *argv* inside the ephemeral sandbox directory.

        Before execution, every argument is checked against blocked_paths.
        If any argument refers to a blocked path, :class:`PermissionError`
        is raised without executing the command.

        Args:
            argv: Command and arguments as a list.
            timeout: Maximum wall-clock seconds before TimeoutExpired.

        Returns:
            Tuple of (returncode, stdout, stderr).

        Raises:
            RuntimeError: If called outside an active context manager.
            PermissionError: If any argv element references a blocked path.
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
        """
        if self._temp_dir is None:
            raise RuntimeError(
                "WindowsSandboxRunner is not active; use it as a context manager"
            )

        # Validate all argv elements for blocked paths
        for arg in argv:
            if self._looks_like_path(arg) and self.read_path_blocked(arg):
                raise PermissionError(
                    f"Argument references a blocked path: {arg!r}"
                )

        return self._executor.execute_shell(
            argv,
            cwd=self._temp_dir,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _looks_like_path(self, arg: str) -> bool:
        """Heuristic: return True if *arg* looks like a file-system path.

        We check for drive letters (Windows absolute), UNC paths, forward
        slashes that suggest a path, or backslashes.  Short tokens that are
        likely flags or values are not treated as paths.
        """
        if len(arg) < 3:
            return False
        # Windows drive letter (C:/, C:\\)
        if len(arg) >= 3 and arg[1:3] in (":/", ":\\"):
            return True
        # UNC path
        if arg.startswith("\\\\") or arg.startswith("//"):
            return True
        # Contains path separators and looks like an absolute or relative path
        if "/" in arg or "\\" in arg:
            return True
        return False

    def _setup(self) -> None:
        """Create the ephemeral directory and copy the repo into it."""
        if self._config.ephemeral_dir is not None:
            self._temp_dir = self._config.ephemeral_dir
            self._owns_temp_dir = False
        else:
            parent_temp = tempfile.mkdtemp(prefix="veronica_win_sandbox_")
            self._owns_temp_dir = True
            dest = Path(parent_temp) / "_repo"
            shutil.copytree(
                self._config.repo_root,
                str(dest),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
            self._temp_dir = str(dest)
            # Remember parent for cleanup
            self._parent_temp = parent_temp

    def _teardown(self) -> None:
        """Remove the ephemeral directory."""
        if self._temp_dir is None:
            return
        if not self._owns_temp_dir:
            self._temp_dir = None
            return

        try:
            parent = getattr(self, "_parent_temp", None)
            if parent:
                shutil.rmtree(parent, ignore_errors=True)
            else:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
        finally:
            self._temp_dir = None


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_windows_sandbox(
    repo_root: str,
    executor: "SecureExecutor",
    extra_blocked_paths: list[str] | None = None,
) -> WindowsSandboxRunner:
    """Create a WindowsSandboxRunner with sensible defaults.

    Args:
        repo_root: Absolute path to the original repository root.
        executor: SecureExecutor for running commands.
        extra_blocked_paths: Additional path prefixes to block, merged with
            the defaults.

    Returns:
        A configured WindowsSandboxRunner (not yet entered).
    """
    blocked = _default_blocked_paths()
    if extra_blocked_paths:
        blocked.extend(extra_blocked_paths)

    config = WindowsSandboxConfig(
        repo_root=repo_root,
        blocked_paths=blocked,
    )
    return WindowsSandboxRunner(config, executor)
