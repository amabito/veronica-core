"""Tests for SandboxRunner (Phase B)."""
from __future__ import annotations

from pathlib import Path

import pytest

from veronica_core.adapter.exec import AdapterConfig, SecureExecutor
from veronica_core.runner.sandbox import SandboxConfig, SandboxRunner
from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import PolicyEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> str:
    """A minimal fake repo root with one file."""
    (tmp_path / "hello.txt").write_text("hello from repo")
    return str(tmp_path)


@pytest.fixture()
def executor(repo_root: str) -> SecureExecutor:
    """SecureExecutor bound to the fake repo root."""
    config = AdapterConfig(
        repo_root=repo_root,
        policy_engine=PolicyEngine(),
        caps=CapabilitySet.dev(),
    )
    return SecureExecutor(config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSandboxIsolation:
    """Verify that sandbox writes do not affect the original repo."""

    def test_write_in_sandbox_does_not_affect_repo(
        self, tmp_path: Path, repo_root: str, executor: SecureExecutor
    ) -> None:
        """A file written inside the sandbox must not appear in repo_root."""
        # Use a script file to avoid the blocked python -c inline-exec flag.
        write_script = tmp_path / "write_file.py"
        write_script.write_text("open('sandbox_output.txt', 'w').write('test')\n")

        sandbox_cfg = SandboxConfig(
            repo_root=repo_root,
            executor=executor,
            read_only=True,
        )
        with SandboxRunner(sandbox_cfg) as runner:
            # Write a file inside the sandbox (python writes to cwd)
            rc, out, err = runner.run_in_sandbox(
                ["python", str(write_script)]
            )
            assert rc == 0, f"sandbox write failed: {err}"
            # Confirm the file exists in the sandbox
            sandbox_path = Path(runner.sandbox_dir) / "sandbox_output.txt"
            assert sandbox_path.exists(), "file should exist in sandbox"

        # After context exit, repo_root must NOT contain the file
        repo_path = Path(repo_root) / "sandbox_output.txt"
        assert not repo_path.exists(), "sandbox write must not leak to repo_root"

    def test_repo_files_available_in_sandbox(
        self, repo_root: str, executor: SecureExecutor, tmp_path: Path
    ) -> None:
        """The sandbox starts with a copy of the repo contents."""
        # Write a helper script so we avoid shell operators in -c string
        script = tmp_path / "list_dir.py"
        script.write_text("import os\nprint(os.listdir('.'))\n")
        sandbox_cfg = SandboxConfig(
            repo_root=repo_root,
            executor=executor,
            read_only=True,
        )
        with SandboxRunner(sandbox_cfg) as runner:
            rc, out, err = runner.run_in_sandbox(["python", str(script)])
            assert rc == 0
            assert "hello.txt" in out


class TestSandboxCleanup:
    """Verify that the sandbox temp directory is removed after exit."""

    def test_temp_dir_deleted_after_exit(
        self, repo_root: str, executor: SecureExecutor
    ) -> None:
        """The ephemeral sandbox directory must not exist after __exit__."""
        sandbox_cfg = SandboxConfig(
            repo_root=repo_root,
            executor=executor,
            read_only=True,
        )
        captured_parent: list[str] = []

        with SandboxRunner(sandbox_cfg) as runner:
            # Record the parent dir (the one we own)
            captured_parent.append(str(Path(runner.sandbox_dir).parent))
            assert Path(runner.sandbox_dir).exists()

        # The parent temp dir should have been deleted
        assert captured_parent, "sandbox_dir parent was not captured"
        assert not Path(captured_parent[0]).exists(), (
            f"temp dir {captured_parent[0]} should have been deleted after exit"
        )

    def test_sandbox_raises_when_used_outside_context(
        self, repo_root: str, executor: SecureExecutor
    ) -> None:
        """run_in_sandbox() must raise RuntimeError outside context manager."""
        sandbox_cfg = SandboxConfig(
            repo_root=repo_root,
            executor=executor,
            read_only=True,
        )
        runner = SandboxRunner(sandbox_cfg)
        with pytest.raises(RuntimeError, match="not active"):
            runner.run_in_sandbox(["python", "--version"])
