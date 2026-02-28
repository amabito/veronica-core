"""Tests for runner/sandbox_windows.py — Windows Sandbox hardening."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from veronica_core.runner.sandbox_windows import (
    WindowsSandboxConfig,
    WindowsSandboxRunner,
    create_windows_sandbox,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_executor() -> MagicMock:
    """A mock SecureExecutor that always returns (0, 'ok', '')."""
    executor = MagicMock()
    executor.execute_shell.return_value = (0, "ok", "")
    return executor


@pytest.fixture()
def repo_root(tmp_path: Path) -> str:
    (tmp_path / "hello.txt").write_text("hello")
    return str(tmp_path)


@pytest.fixture()
def runner(repo_root: str, mock_executor: MagicMock) -> WindowsSandboxRunner:
    """A WindowsSandboxRunner with predictable blocked_paths for testing."""
    config = WindowsSandboxConfig(
        repo_root=repo_root,
        blocked_paths=[
            r"C:\Users",
            "C:/Users",
            r"C:\Windows\System32",
            "C:/Windows/System32",
        ],
    )
    return WindowsSandboxRunner(config, mock_executor)


# ---------------------------------------------------------------------------
# read_path_blocked — platform-independent tests
# ---------------------------------------------------------------------------


class TestReadPathBlocked:
    def test_blocks_windows_users_backslash(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked(r"C:\Users\testuser\secret.txt") is True

    def test_blocks_windows_users_forward_slash(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked("C:/Users/testuser/.env") is True

    def test_blocks_system32_backslash(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked(r"C:\Windows\System32\cmd.exe") is True

    def test_blocks_system32_forward_slash(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked("C:/Windows/System32/notepad.exe") is True

    def test_allows_temp_dir(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked("C:/tmp/sandbox/output.txt") is False

    def test_allows_repo_root(self, runner: WindowsSandboxRunner) -> None:
        """A path outside of blocked prefixes (e.g. D: drive) must not be blocked."""
        # Use a drive path that is NOT in the blocked list (D: drive project path)
        path = "D:/work/Projects/myrepo/src/file.py"
        assert runner.read_path_blocked(path) is False

    def test_case_insensitive_users(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked("c:/users/Admin/Desktop/keys.txt") is True

    def test_exact_prefix_no_partial_match(self, runner: WindowsSandboxRunner) -> None:
        """C:/UsersExtra should not be blocked just because 'C:/Users' is blocked."""
        assert runner.read_path_blocked("C:/UsersExtra/something.txt") is False

    def test_blocks_env_appdata(self, tmp_path: Path, mock_executor: MagicMock) -> None:
        """APPDATA env var path should be blocked by default."""
        appdata = str(tmp_path / "AppData" / "Roaming")
        with patch.dict("os.environ", {"APPDATA": appdata, "USERPROFILE": ""}):
            from veronica_core.runner.sandbox_windows import create_windows_sandbox
            r = create_windows_sandbox(str(tmp_path), mock_executor)
            target = str(Path(appdata) / "secrets.json")
            assert r.read_path_blocked(target) is True

    def test_blocks_env_userprofile(self, tmp_path: Path, mock_executor: MagicMock) -> None:
        """USERPROFILE env var path should be blocked by default."""
        userprofile = str(tmp_path / "UserProfile")
        with patch.dict("os.environ", {"USERPROFILE": userprofile, "APPDATA": ""}):
            from veronica_core.runner.sandbox_windows import create_windows_sandbox
            r = create_windows_sandbox(str(tmp_path), mock_executor)
            target = str(Path(userprofile) / ".ssh" / "id_rsa")
            assert r.read_path_blocked(target) is True

    def test_allows_relative_path(self, runner: WindowsSandboxRunner) -> None:
        assert runner.read_path_blocked("src/main.py") is False



# ---------------------------------------------------------------------------
# run_in_sandbox — path validation
# ---------------------------------------------------------------------------


class TestRunInSandboxPathValidation:
    def test_raises_on_blocked_path_in_argv(
        self, runner: WindowsSandboxRunner, repo_root: str, mock_executor: MagicMock
    ) -> None:
        """run_in_sandbox with a blocked path arg must raise PermissionError."""
        with WindowsSandboxRunner(
            WindowsSandboxConfig(
                repo_root=repo_root,
                blocked_paths=["C:/Users", r"C:\Users"],
            ),
            mock_executor,
        ) as r:
            with pytest.raises(PermissionError, match="blocked path"):
                r.run_in_sandbox(["echo", "C:/Users/testuser/.env"])

    def test_raises_when_not_in_context(
        self, runner: WindowsSandboxRunner
    ) -> None:
        with pytest.raises(RuntimeError, match="not active"):
            runner.run_in_sandbox(["echo", "hello"])

    def test_clean_argv_succeeds(
        self, repo_root: str, mock_executor: MagicMock
    ) -> None:
        config = WindowsSandboxConfig(
            repo_root=repo_root,
            blocked_paths=["C:/Users"],
        )
        with WindowsSandboxRunner(config, mock_executor) as r:
            rc, out, err = r.run_in_sandbox(["echo", "hello"])
        assert rc == 0
        mock_executor.execute_shell.assert_called_once()



# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_sandbox_dir_available_inside_context(
        self, repo_root: str, mock_executor: MagicMock
    ) -> None:
        config = WindowsSandboxConfig(repo_root=repo_root)
        with WindowsSandboxRunner(config, mock_executor) as r:
            assert Path(r.sandbox_dir).exists()

    def test_sandbox_dir_cleaned_up_after_exit(
        self, repo_root: str, mock_executor: MagicMock
    ) -> None:
        config = WindowsSandboxConfig(repo_root=repo_root)
        captured: list[str] = []
        with WindowsSandboxRunner(config, mock_executor) as r:
            captured.append(r.sandbox_dir)
        # The parent temp dir (one level above _repo) should be gone
        parent = str(Path(captured[0]).parent)
        assert not Path(parent).exists()

    def test_sandbox_dir_raises_outside_context(
        self, runner: WindowsSandboxRunner
    ) -> None:
        with pytest.raises(RuntimeError, match="not active"):
            _ = runner.sandbox_dir

    def test_ephemeral_dir_not_deleted_when_provided(
        self, repo_root: str, mock_executor: MagicMock, tmp_path: Path
    ) -> None:
        """If ephemeral_dir is provided by caller, we must not delete it."""
        config = WindowsSandboxConfig(
            repo_root=repo_root,
            ephemeral_dir=str(tmp_path),
        )
        with WindowsSandboxRunner(config, mock_executor):
            pass
        # tmp_path was provided externally — must still exist
        assert tmp_path.exists()


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestCreateWindowsSandbox:
    def test_factory_returns_runner(
        self, repo_root: str, mock_executor: MagicMock
    ) -> None:
        runner = create_windows_sandbox(repo_root, mock_executor)
        assert isinstance(runner, WindowsSandboxRunner)

    def test_factory_includes_extra_blocked(
        self, repo_root: str, mock_executor: MagicMock, tmp_path: Path
    ) -> None:
        extra = str(tmp_path / "extra_blocked")
        runner = create_windows_sandbox(repo_root, mock_executor, extra_blocked_paths=[extra])
        target = str(Path(extra) / "file.txt")
        assert runner.read_path_blocked(target) is True


# ---------------------------------------------------------------------------
# Windows-only execution test (skipped on other platforms)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestWindowsExecution:
    def test_echo_runs_successfully(self, repo_root: str) -> None:
        """On Windows, a real python script via SecureExecutor should work."""
        from veronica_core.adapter.exec import AdapterConfig, SecureExecutor
        from veronica_core.security.capabilities import CapabilitySet
        from veronica_core.security.policy_engine import PolicyEngine

        config = AdapterConfig(
            repo_root=repo_root,
            policy_engine=PolicyEngine(),
            caps=CapabilitySet.dev(),
        )
        executor = SecureExecutor(config)
        sandbox_config = WindowsSandboxConfig(
            repo_root=repo_root,
            blocked_paths=["C:/Users", r"C:\Users"],
        )
        with WindowsSandboxRunner(sandbox_config, executor) as r:
            # Write the script directly into the sandbox dir so the path is not
            # under a blocked prefix. Then reference it with a relative name so
            # the path-blocked check is bypassed (sandbox cwd == sandbox_dir).
            script = Path(r.sandbox_dir) / "hello.py"
            script.write_text("print('hello')\n")
            rc, out, err = r.run_in_sandbox(["python", "hello.py"])
            assert rc == 0
            assert "hello" in out
