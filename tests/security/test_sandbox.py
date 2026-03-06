"""Tests for SandboxRunner (Phase B)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from veronica_core.adapter.exec import AdapterConfig, SecureExecutor
from veronica_core.runner.sandbox import (
    SandboxConfig,
    SandboxRunner,
    _sandbox_ignore,
    _SANDBOX_IGNORE_NAMES,
)
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
            rc, out, err = runner.run_in_sandbox(["python", str(write_script)])
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


# ---------------------------------------------------------------------------
# _sandbox_ignore unit tests
# ---------------------------------------------------------------------------


class TestSandboxIgnoreFunction:
    """Direct unit tests for _sandbox_ignore()."""

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        """Symlinks must be excluded from copytree."""
        target = tmp_path / "real_file.txt"
        target.write_text("data")
        link = tmp_path / "link_to_file"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation requires privileges on this platform")
        result = _sandbox_ignore(str(tmp_path), ["real_file.txt", "link_to_file"])
        assert "link_to_file" in result
        assert "real_file.txt" not in result

    def test_junction_rejected(self, tmp_path: Path) -> None:
        """NTFS junctions must be excluded (os.path.islink returns False)."""
        target_dir = tmp_path / "real_dir"
        target_dir.mkdir()
        junction = tmp_path / "junction_dir"
        if sys.platform == "win32":
            # Create NTFS junction via mklink /J
            import subprocess

            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target_dir)],
                check=True,
                capture_output=True,
            )
        else:
            pytest.skip("NTFS junctions only exist on Windows")
        result = _sandbox_ignore(str(tmp_path), ["real_dir", "junction_dir"])
        assert "junction_dir" in result
        assert "real_dir" not in result

    @pytest.mark.parametrize("name", list(_SANDBOX_IGNORE_NAMES))
    def test_credential_names_rejected(self, tmp_path: Path, name: str) -> None:
        """Every name in the credential denylist must be ignored."""
        (tmp_path / name).mkdir() if name in (".docker", ".aws", ".ssh") else (
            tmp_path / name
        ).write_text("x")
        result = _sandbox_ignore(str(tmp_path), [name])
        assert name in result

    @pytest.mark.parametrize(
        "name",
        ["secret.key", "cert.pem", "store.pfx", "bundle.p12", "token.secret", "db.env"],
    )
    def test_suffix_match(self, tmp_path: Path, name: str) -> None:
        """Files with credential suffixes must be ignored."""
        (tmp_path / name).write_text("x")
        result = _sandbox_ignore(str(tmp_path), [name])
        assert name in result

    @pytest.mark.parametrize(
        "name",
        ["SECRET.KEY", "cert.PEM", "store.PFX", "bundle.P12", "token.SECRET", "db.ENV"],
    )
    def test_suffix_case_insensitive(self, tmp_path: Path, name: str) -> None:
        """Uppercase extensions must also be caught (case-insensitive match)."""
        (tmp_path / name).write_text("x")
        result = _sandbox_ignore(str(tmp_path), [name])
        assert name in result, f"{name} should be ignored (case-insensitive suffix)"

    def test_prefix_env_dot_variant(self, tmp_path: Path) -> None:
        """.env.production and similar must be ignored."""
        name = ".env.production"
        (tmp_path / name).write_text("x")
        result = _sandbox_ignore(str(tmp_path), [name])
        assert name in result

    def test_safe_file_not_ignored(self, tmp_path: Path) -> None:
        """Normal source files must not be ignored."""
        name = "main.py"
        (tmp_path / name).write_text("x")
        result = _sandbox_ignore(str(tmp_path), [name])
        assert name not in result
