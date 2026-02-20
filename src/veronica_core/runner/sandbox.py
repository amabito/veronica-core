"""Sandbox runner for VERONICA Security Containment Layer.

Provides an ephemeral working directory isolated from the original repo.
Writes in the sandbox do NOT propagate to the original repository.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.adapter.exec import SecureExecutor


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SandboxConfig:
    """Configuration for SandboxRunner.

    Args:
        repo_root: Absolute path to the original repository root.
        executor: SecureExecutor used to run commands inside the sandbox.
        ephemeral_dir: If provided, use this directory as the sandbox root
            instead of creating a new temp directory. The directory is
            still cleaned up on exit unless it was pre-existing.
        read_only: When True (default), the original repo is copied into a
            fresh temp directory so writes cannot affect it.
    """

    repo_root: str
    executor: "SecureExecutor"
    ephemeral_dir: str | None = None
    read_only: bool = True


# ---------------------------------------------------------------------------
# SandboxRunner
# ---------------------------------------------------------------------------


class SandboxRunner:
    """Context manager that runs commands in an ephemeral sandbox directory.

    On entry:
        - A temporary directory is created (or the configured *ephemeral_dir*
          is used).
        - The original repo is copied into the temp directory so that writes
          remain isolated.

    On exit:
        - The temporary directory is deleted, leaving the original repo
          unchanged.

    Usage::

        config = SandboxConfig(repo_root="/path/to/repo", executor=my_executor)
        with SandboxRunner(config) as runner:
            rc, out, err = runner.run_in_sandbox(["pytest", "tests/"])

    Args:
        config: SandboxConfig controlling sandbox behaviour.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._temp_dir: str | None = None
        self._owns_temp_dir: bool = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SandboxRunner":
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
            raise RuntimeError("SandboxRunner is not active; use it as a context manager")
        return self._temp_dir

    def run_in_sandbox(
        self,
        argv: list[str],
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """Execute *argv* inside the ephemeral sandbox directory.

        Policy checks are applied by the underlying SecureExecutor.
        All I/O is directed to the ephemeral directory; the original
        repo_root is never modified.

        Args:
            argv: Command and arguments as a list.
            timeout: Maximum wall-clock seconds before TimeoutExpired.

        Returns:
            Tuple of (returncode, stdout, stderr).

        Raises:
            RuntimeError: If called outside an active context manager.
            SecurePermissionError: If PolicyEngine returns DENY.
            ApprovalRequiredError: If PolicyEngine returns REQUIRE_APPROVAL.
        """
        if self._temp_dir is None:
            raise RuntimeError("SandboxRunner is not active; use it as a context manager")

        return self._config.executor.execute_shell(
            argv,
            cwd=self._temp_dir,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        """Create the ephemeral directory and optionally copy the repo."""
        if self._config.ephemeral_dir is not None:
            self._temp_dir = self._config.ephemeral_dir
            self._owns_temp_dir = False
        else:
            self._temp_dir = tempfile.mkdtemp(prefix="veronica_sandbox_")
            self._owns_temp_dir = True

        if self._config.read_only:
            dest = Path(self._temp_dir) / "_repo"
            shutil.copytree(
                self._config.repo_root,
                str(dest),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
            self._temp_dir = str(dest)

    def _teardown(self) -> None:
        """Remove the ephemeral directory."""
        if self._temp_dir is None:
            return
        if not self._owns_temp_dir:
            return

        # Walk up to the temp root we own (not the _repo subdirectory).
        temp_root = self._temp_dir
        if self._config.read_only:
            temp_root = str(Path(self._temp_dir).parent)

        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        finally:
            self._temp_dir = None
