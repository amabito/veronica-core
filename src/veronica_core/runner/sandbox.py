"""Sandbox runner for VERONICA Security Containment Layer.

Provides an ephemeral working directory isolated from the original repo.
Writes in the sandbox do NOT propagate to the original repository.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from veronica_core.adapters.exec import SecureExecutor


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
# Sandbox ignore function
# ---------------------------------------------------------------------------

# Patterns matched against basename only (like shutil.ignore_patterns).
_SANDBOX_IGNORE_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        # Secrets and credentials
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".docker",
        ".aws",
        ".ssh",
        "credentials.json",
    }
)

# Glob-style suffix/prefix patterns.
_SANDBOX_IGNORE_SUFFIXES: tuple[str, ...] = (
    ".pyc",
    ".env",
    ".key",
    ".pem",
    ".pfx",
    ".p12",
    ".secret",
)

_SANDBOX_IGNORE_PREFIXES: tuple[str, ...] = (".env.",)


def _is_junction(path: str) -> bool:
    """Return True if *path* is an NTFS junction (reparse point).

    ``os.path.islink()`` returns False for NTFS junctions on Python < 3.12.
    ``Path.is_junction()`` was added in Python 3.12.  This helper works on
    all supported Python versions by trying the stdlib API first, then
    falling back to a ctypes ``GetFileAttributesW`` check on Windows.
    """
    # Python 3.12+ has Path.is_junction()
    try:
        return Path(path).is_junction()
    except AttributeError:
        pass

    # Fallback for Python < 3.12 on Windows
    if os.name == "nt":
        try:
            import ctypes

            FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(path)  # type: ignore[union-attr]
            if attrs == -1:
                return False
            return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        except (OSError, AttributeError):
            return False

    return False


def _sandbox_ignore(directory: str, names: list[str]) -> set[str]:
    """Ignore function for shutil.copytree that also rejects symlinks/junctions."""
    ignored: set[str] = set()
    for name in names:
        full_path = os.path.join(directory, name)
        # Reject symlinks to prevent sandbox escape via link traversal.
        if os.path.islink(full_path):
            ignored.add(name)
            continue
        # Reject NTFS junctions (os.path.islink returns False for junctions).
        if _is_junction(full_path):
            ignored.add(name)
            continue
        if name in _SANDBOX_IGNORE_NAMES:
            ignored.add(name)
            continue
        # Case-insensitive suffix match to prevent bypass via uppercase
        # extensions on case-insensitive filesystems (e.g. secret.PEM).
        name_lower = name.lower()
        if name_lower.endswith(_SANDBOX_IGNORE_SUFFIXES):
            ignored.add(name)
            continue
        if name_lower.startswith(_SANDBOX_IGNORE_PREFIXES):
            ignored.add(name)
            continue
    return ignored


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
            raise RuntimeError(
                "SandboxRunner is not active; use it as a context manager"
            )
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
            raise RuntimeError(
                "SandboxRunner is not active; use it as a context manager"
            )

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
            # Remove any stale data from a previous run before copying.
            # dirs_exist_ok=True would silently merge old and new files, allowing
            # deleted-source files to persist in the sandbox (data contamination).
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(
                self._config.repo_root,
                str(dest),
                dirs_exist_ok=False,
                ignore=_sandbox_ignore,
            )
            self._temp_dir = str(dest)

    def _teardown(self) -> None:
        """Remove the ephemeral directory."""
        if self._temp_dir is None:
            return
        if not self._owns_temp_dir:
            self._temp_dir = None
            return

        # Walk up to the temp root we own (not the _repo subdirectory).
        temp_root = self._temp_dir
        if self._config.read_only:
            temp_root = str(Path(self._temp_dir).parent)

        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        finally:
            self._temp_dir = None
