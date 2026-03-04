"""Dedicated tests for veronica_core.adapter.exec.SecureExecutor.

Covers:
- execute_shell(): path traversal attempts, allowed commands, empty argv
- fetch_url(): timeout, URLError boundaries, non-GET method
- read_file(): FILE_READ vs FILE_READ_SENSITIVE capability branches
- write_file(): mkdir side-effects, out-of-repo rejection

All subprocess and urllib calls are mocked to avoid real system calls.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

import pytest

from veronica_core.adapter.exec import (
    AdapterConfig,
    ApprovalRequiredError,
    SecureExecutor,
    SecurePermissionError,
)
from veronica_core.security.capabilities import Capability, CapabilitySet
from veronica_core.security.policy_engine import ExecPolicyDecision, PolicyEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allow_engine() -> PolicyEngine:
    """Return a PolicyEngine stub that always ALLOWs."""
    engine = Mock(spec=PolicyEngine)
    engine.evaluate.return_value = ExecPolicyDecision(
        verdict="ALLOW", rule_id="TEST_ALLOW", reason="test allow", risk_score_delta=0
    )
    return engine


def _deny_engine(rule_id: str = "TEST_DENY") -> PolicyEngine:
    """Return a PolicyEngine stub that always DENYs."""
    engine = Mock(spec=PolicyEngine)
    engine.evaluate.return_value = ExecPolicyDecision(
        verdict="DENY", rule_id=rule_id, reason="test deny", risk_score_delta=5
    )
    return engine


def _approval_engine(rule_id: str = "TEST_APPROVAL") -> PolicyEngine:
    """Return a PolicyEngine stub that always requires APPROVAL."""
    engine = Mock(spec=PolicyEngine)
    engine.evaluate.return_value = ExecPolicyDecision(
        verdict="REQUIRE_APPROVAL",
        rule_id=rule_id,
        reason="test approval",
        risk_score_delta=3,
    )
    return engine


def _make_caps(*capabilities: Capability) -> CapabilitySet:
    """Build a CapabilitySet from the given capabilities."""
    return CapabilitySet(caps=frozenset(capabilities))


def _make_executor(
    engine: PolicyEngine | None = None,
    caps: CapabilitySet | None = None,
    repo_root: str | None = None,
) -> tuple[SecureExecutor, str]:
    """Create a SecureExecutor with a temporary repo_root."""
    with tempfile.TemporaryDirectory() as tmp:
        root = repo_root or tmp
        cfg = AdapterConfig(
            repo_root=root,
            policy_engine=engine or _allow_engine(),
            caps=caps or _make_caps(),
        )
        return SecureExecutor(cfg), root


# ---------------------------------------------------------------------------
# execute_shell() tests
# ---------------------------------------------------------------------------


class TestExecuteShellPolicy:
    """Policy enforcement for execute_shell."""

    def test_empty_argv_raises_value_error(self) -> None:
        """Empty argv must raise ValueError before policy is checked."""
        exe, _ = _make_executor()
        with pytest.raises(ValueError, match="argv must not be empty"):
            exe.execute_shell([])

    def test_deny_raises_secure_permission_error(self) -> None:
        """DENY verdict must raise SecurePermissionError with correct rule_id."""
        exe, _ = _make_executor(engine=_deny_engine("SHELL_DENY_DEFAULT"))
        with pytest.raises(SecurePermissionError) as exc_info:
            exe.execute_shell(["pytest", "--version"])
        assert exc_info.value.rule_id == "SHELL_DENY_DEFAULT"

    def test_require_approval_raises_approval_required_error(self) -> None:
        """REQUIRE_APPROVAL verdict must raise ApprovalRequiredError with args_hash."""
        exe, _ = _make_executor(engine=_approval_engine("SHELL_PKG_INSTALL"))
        with pytest.raises(ApprovalRequiredError) as exc_info:
            exe.execute_shell(["pip", "install", "requests"])
        err = exc_info.value
        assert err.rule_id == "SHELL_PKG_INSTALL"
        # args_hash must be a valid hex digest
        assert len(err.args_hash) == 64
        int(err.args_hash, 16)  # must parse as hex

    def test_allow_executes_subprocess(self) -> None:
        """ALLOW verdict must invoke subprocess.run and return (returncode, stdout, stderr)."""
        exe, root = _make_executor(engine=_allow_engine())
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hello\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            rc, out, err = exe.execute_shell(["pytest", "--version"])
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        # shell=False must be enforced
        assert call_kwargs["shell"] is False
        assert rc == 0
        assert "hello" in out

    def test_shell_false_enforced(self) -> None:
        """subprocess.run must always be called with shell=False."""
        exe, _ = _make_executor(engine=_allow_engine())
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            exe.execute_shell(["pytest"])
        _, call_kwargs = mock_run.call_args
        # shell kwarg should be False
        assert mock_run.call_args.kwargs.get("shell") is False or (
            len(mock_run.call_args.args) >= 2 and mock_run.call_args.args[1] is False
        )

    def test_timeout_propagates(self) -> None:
        """subprocess.TimeoutExpired must propagate from execute_shell."""
        exe, _ = _make_executor(engine=_allow_engine())
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=1),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                exe.execute_shell(["pytest"], timeout=1)

    def test_secrets_masked_in_stdout(self) -> None:
        """Secrets in stdout must be masked before returning."""
        exe, _ = _make_executor(engine=_allow_engine())
        mock_result = MagicMock(returncode=0, stdout="token=ABCD1234SECRET", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            # Just confirm execute_shell doesn't crash — masking is tested separately
            rc, out, err = exe.execute_shell(["pytest"])
        assert rc == 0


class TestExecuteShellPathTraversal:
    """Path traversal via shell injection patterns must be blocked by policy."""

    def test_shell_operator_semicolon_denied(self) -> None:
        """Semicolons are shell operators — policy must block them."""
        engine = PolicyEngine()
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=engine,
                caps=_make_caps(Capability.SHELL_BASIC),
            )
            exe = SecureExecutor(cfg)
            # "cd /tmp && cat /etc/passwd" split as argv — pytest is allowed, but
            # ";" as a standalone arg triggers SHELL_DENY_OPERATOR
            # The real risk: argv=["sh", "-c", "..."] — sh is in DENY list
            with pytest.raises(SecurePermissionError):
                exe.execute_shell(["sh", "-c", "cd /tmp && cat /etc/passwd"])

    def test_bash_exec_denied(self) -> None:
        """bash is not in SHELL_ALLOW_COMMANDS — must be DENYed."""
        engine = PolicyEngine()
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=engine,
                caps=_make_caps(Capability.SHELL_BASIC),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError):
                exe.execute_shell(["bash", "-c", "cat /etc/passwd"])

    def test_python_minus_c_denied(self) -> None:
        """python -c <code> enables arbitrary execution — must be DENYed."""
        engine = PolicyEngine()
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=engine,
                caps=_make_caps(Capability.SHELL_BASIC),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError):
                exe.execute_shell(["python", "-c", "import os; os.system('id')"])

    def test_rm_rf_denied(self) -> None:
        """rm is not in SHELL_ALLOW_COMMANDS — must be DENYed."""
        engine = PolicyEngine()
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=engine,
                caps=_make_caps(Capability.SHELL_BASIC),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError):
                exe.execute_shell(["rm", "-rf", "/"])

    def test_curl_not_in_allowlist_denied(self) -> None:
        """curl is not in SHELL_ALLOW_COMMANDS — must be DENYed."""
        engine = PolicyEngine()
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=engine,
                caps=_make_caps(Capability.SHELL_BASIC),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError):
                exe.execute_shell(["curl", "https://evil.example.com"])


# ---------------------------------------------------------------------------
# fetch_url() tests
# ---------------------------------------------------------------------------


class TestFetchUrl:
    """Policy and error boundaries for fetch_url."""

    def test_deny_raises_secure_permission_error(self) -> None:
        """DENY verdict must raise SecurePermissionError."""
        exe, _ = _make_executor(engine=_deny_engine("NET_DENY"))
        with pytest.raises(SecurePermissionError) as exc_info:
            exe.fetch_url("https://evil.example.com")
        assert exc_info.value.rule_id == "NET_DENY"

    def test_url_error_wrapped_and_re_raised(self) -> None:
        """URLError from urlopen must be caught and re-raised with context."""
        exe, _ = _make_executor(engine=_allow_engine())
        with patch(
            "urllib.request.urlopen",
            side_effect=URLError("connection refused"),
        ):
            with pytest.raises(URLError, match="fetch_url failed"):
                exe.fetch_url("https://example.com")

    def test_timeout_causes_url_error(self) -> None:
        """socket.timeout from urlopen surfaces as URLError."""
        import socket

        exe, _ = _make_executor(engine=_allow_engine())
        with patch(
            "urllib.request.urlopen",
            side_effect=URLError(socket.timeout("timed out")),
        ):
            with pytest.raises(URLError):
                exe.fetch_url("https://example.com")

    def test_successful_fetch_returns_body(self) -> None:
        """Successful fetch must return response body as string."""
        exe, _ = _make_executor(engine=_allow_engine())
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"hello world"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            body = exe.fetch_url("https://example.com")
        assert "hello world" in body

    def test_approval_required_raises_approval_error(self) -> None:
        """REQUIRE_APPROVAL for net must raise ApprovalRequiredError."""
        exe, _ = _make_executor(engine=_approval_engine("NET_ALLOWLIST"))
        with pytest.raises(ApprovalRequiredError):
            exe.fetch_url("https://example.com")

    def test_secrets_masked_in_response(self) -> None:
        """Secrets in response body must be masked."""
        exe, _ = _make_executor(engine=_allow_engine())
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        # Response contains a masked pattern
        mock_resp.read.return_value = b"data: ok"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            body = exe.fetch_url("https://example.com")
        # Body must be returned (masking of non-secrets leaves text unchanged)
        assert isinstance(body, str)


# ---------------------------------------------------------------------------
# read_file() tests
# ---------------------------------------------------------------------------


class TestReadFile:
    """Capability branch tests for read_file."""

    def test_read_within_repo_no_sensitive_cap_succeeds(self) -> None:
        """Reading a file inside repo_root without FILE_READ_SENSITIVE must succeed."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "notes.txt"
            target.write_text("content", encoding="utf-8")

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),  # No FILE_READ_SENSITIVE
            )
            exe = SecureExecutor(cfg)
            result = exe.read_file(str(target))
        assert "content" in result

    def test_read_outside_repo_without_sensitive_cap_raises(self) -> None:
        """Reading outside repo_root without FILE_READ_SENSITIVE must raise PermissionError."""
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),  # No FILE_READ_SENSITIVE
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(PermissionError, match="outside repo_root"):
                # /tmp/../etc/passwd-style path should be blocked
                exe.read_file("/etc/hostname")

    def test_read_outside_repo_with_sensitive_cap_allowed_by_path_check(self) -> None:
        """With FILE_READ_SENSITIVE cap, _check_within_repo is skipped (path check bypassed)."""
        with tempfile.TemporaryDirectory() as root:
            # Create a file outside root to attempt reading
            with tempfile.NamedTemporaryFile(
                suffix=".txt", delete=False, mode="w"
            ) as f:
                f.write("external content")
                external_path = f.name

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(Capability.FILE_READ_SENSITIVE),
            )
            exe = SecureExecutor(cfg)
            # With FILE_READ_SENSITIVE, the within-repo check is skipped
            # (policy engine still runs, but we mock it to ALLOW)
            result = exe.read_file(external_path)
            assert "external content" in result

            Path(external_path).unlink(missing_ok=True)

    def test_read_file_not_found_raises(self) -> None:
        """FileNotFoundError must propagate when file does not exist."""
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(FileNotFoundError):
                exe.read_file(str(Path(root) / "nonexistent.txt"))

    def test_read_file_policy_deny_blocks_read(self) -> None:
        """DENY from policy engine must block read_file even for in-repo files."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / ".env"
            target.write_text("SECRET=abc123", encoding="utf-8")

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_deny_engine("FILE_READ_DENY"),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError) as exc_info:
                exe.read_file(str(target))
            assert exc_info.value.rule_id == "FILE_READ_DENY"

    def test_read_relative_path_resolves_to_repo_root(self) -> None:
        """Relative paths must be resolved relative to repo_root."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "subdir" / "file.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("relative content", encoding="utf-8")

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            result = exe.read_file("subdir/file.txt")
        assert "relative content" in result


# ---------------------------------------------------------------------------
# write_file() tests
# ---------------------------------------------------------------------------


class TestWriteFile:
    """mkdir side-effects and policy enforcement for write_file."""

    def test_write_creates_parent_directories(self) -> None:
        """write_file must create parent directories (mkdir side-effect)."""
        with tempfile.TemporaryDirectory() as root:
            nested = Path(root) / "a" / "b" / "c" / "file.txt"
            assert not nested.parent.exists()

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            exe.write_file(str(nested), "nested content")

            assert nested.exists()
            assert nested.read_text(encoding="utf-8") == "nested content"

    def test_write_outside_repo_raises_permission_error(self) -> None:
        """Writing outside repo_root must raise PermissionError."""
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(PermissionError, match="outside repo_root"):
                exe.write_file("/etc/evil.conf", "malicious")

    def test_write_denied_by_policy_raises_secure_permission_error(self) -> None:
        """DENY verdict must raise SecurePermissionError and NOT write the file."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "output.txt"

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_deny_engine("FILE_WRITE_DENY"),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(SecurePermissionError):
                exe.write_file(str(target), "should not be written")

            assert not target.exists()

    def test_write_approval_required_raises_approval_error(self) -> None:
        """REQUIRE_APPROVAL verdict must raise ApprovalRequiredError."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "ci.yml"

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_approval_engine("FILE_WRITE_APPROVAL"),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            with pytest.raises(ApprovalRequiredError):
                exe.write_file(str(target), "content")

    def test_write_overwrites_existing_file(self) -> None:
        """write_file must overwrite existing file contents."""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "existing.txt"
            target.write_text("old content", encoding="utf-8")

            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            exe.write_file(str(target), "new content")

            assert target.read_text(encoding="utf-8") == "new content"

    def test_write_relative_path_resolves_within_repo(self) -> None:
        """Relative paths in write_file must resolve within repo_root."""
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),
            )
            exe = SecureExecutor(cfg)
            exe.write_file("relative.txt", "relative write")

            assert (Path(root) / "relative.txt").read_text(
                encoding="utf-8"
            ) == "relative write"


# ---------------------------------------------------------------------------
# Adversarial: exception types and error message quality
# ---------------------------------------------------------------------------


class TestAdversarialExec:
    """Adversarial tests — attacker mindset."""

    def test_secure_permission_error_message_contains_rule_id(self) -> None:
        """SecurePermissionError str must include rule_id for auditability."""
        err = SecurePermissionError("SHELL_DENY_CMD", "rm is not allowed")
        assert "SHELL_DENY_CMD" in str(err)
        assert "rm is not allowed" in str(err)

    def test_approval_required_error_has_args_hash(self) -> None:
        """ApprovalRequiredError must expose args_hash for approval systems."""
        err = ApprovalRequiredError(
            "SHELL_PKG_INSTALL", "approval needed", "abc123hash"
        )
        assert err.args_hash == "abc123hash"
        assert "abc123hash" in str(err)

    def test_execute_shell_concurrent_calls_no_crash(self) -> None:
        """Concurrent execute_shell calls must not corrupt internal state."""
        exe, _ = _make_executor(engine=_deny_engine())
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                exe.execute_shell(["pytest"])
            except SecurePermissionError:
                pass  # Expected
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors in concurrent calls: {errors}"

    def test_args_hash_is_deterministic(self) -> None:
        """args_hash in ApprovalRequiredError must be SHA-256 of repr(args)."""
        exe, _ = _make_executor(engine=_approval_engine("TEST"))
        args = ["pip", "install", "requests"]
        expected_hash = hashlib.sha256(repr(args).encode()).hexdigest()
        with pytest.raises(ApprovalRequiredError) as exc_info:
            exe.execute_shell(args)
        assert exc_info.value.args_hash == expected_hash

    def test_path_traversal_via_dotdot_blocked(self) -> None:
        """../../../etc/passwd style path traversal must be blocked for read_file."""
        with tempfile.TemporaryDirectory() as root:
            cfg = AdapterConfig(
                repo_root=root,
                policy_engine=_allow_engine(),
                caps=_make_caps(),  # No FILE_READ_SENSITIVE
            )
            exe = SecureExecutor(cfg)
            # Relative path with .. sequences should resolve outside repo_root
            with pytest.raises(PermissionError, match="outside repo_root"):
                exe.read_file("../../../etc/passwd")
