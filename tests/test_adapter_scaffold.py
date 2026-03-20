"""Tests for the adapter scaffold generator (veronica_core.cli.new_adapter).

Verifies that generate_adapter() produces well-formed, importable, ruff-clean
adapter and test skeletons.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from veronica_core.cli.new_adapter import _to_pascal_case, generate_adapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ruff_available() -> bool:
    """Return True if ruff is available (binary or Python module)."""
    import shutil

    if shutil.which("ruff"):
        return True
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _ruff_check(path: Path) -> subprocess.CompletedProcess[str]:
    """Run ruff check on *path* and return the completed process.

    Uses ``shutil.which("ruff")`` first so the test works even when ruff
    is installed as a standalone binary rather than as a Python module.
    """
    import shutil

    ruff_bin = shutil.which("ruff")
    if ruff_bin:
        cmd = [ruff_bin, "check", str(path)]
    else:
        cmd = [sys.executable, "-m", "ruff", "check", str(path)]
    return subprocess.run(cmd, capture_output=True, text=True)


_SKIP_NO_RUFF = pytest.mark.skipif(not _ruff_available(), reason="ruff not installed")


# ---------------------------------------------------------------------------
# Core generation tests (8 required)
# ---------------------------------------------------------------------------


class TestGenerateAdapter:
    def test_generate_adapter_valid_name_creates_two_files(
        self, tmp_path: Path
    ) -> None:
        """Generating an adapter with a valid name must return exactly two paths."""
        paths = generate_adapter("testfw", tmp_path)
        assert len(paths) == 2
        for p in paths:
            assert p.exists(), f"Expected file not created: {p}"

    def test_generated_adapter_file_has_correct_pascal_class_name(
        self, tmp_path: Path
    ) -> None:
        """The adapter file must contain a class named <PascalCase>Adapter."""
        generate_adapter("myframework", tmp_path)
        adapter_src = (tmp_path / "adapters" / "myframework.py").read_text()
        assert "class MyframeworkAdapter:" in adapter_src

    def test_generated_test_file_exists(self, tmp_path: Path) -> None:
        """The test file must be created at tests/test_{name}_adapter.py."""
        generate_adapter("testfw", tmp_path)
        test_file = tmp_path / "tests" / "test_testfw_adapter.py"
        assert test_file.exists()

    @_SKIP_NO_RUFF
    def test_generated_adapter_passes_ruff_check(self, tmp_path: Path) -> None:
        """The generated adapter file must pass ruff check with no errors."""
        generate_adapter("cleanfw", tmp_path)
        adapter_path = tmp_path / "adapters" / "cleanfw.py"
        result = _ruff_check(adapter_path)
        assert result.returncode == 0, (
            f"ruff reported errors in generated adapter:\n{result.stdout}\n{result.stderr}"
        )

    @_SKIP_NO_RUFF
    def test_generated_test_file_passes_ruff_check(self, tmp_path: Path) -> None:
        """The generated test file must pass ruff check with no errors."""
        generate_adapter("cleanfw", tmp_path)
        test_path = tmp_path / "tests" / "test_cleanfw_adapter.py"
        result = _ruff_check(test_path)
        assert result.returncode == 0, (
            f"ruff reported errors in generated test file:\n{result.stdout}\n{result.stderr}"
        )

    def test_generated_adapter_is_importable_via_compile(self, tmp_path: Path) -> None:
        """The generated adapter source must compile without SyntaxError."""
        generate_adapter("execfw", tmp_path)
        source = (tmp_path / "adapters" / "execfw.py").read_text(encoding="utf-8")
        # compile() raises SyntaxError for malformed source
        compiled = compile(source, "<generated>", "exec")
        assert compiled is not None

    def test_invalid_framework_name_raises_value_error(self, tmp_path: Path) -> None:
        """Names starting with a digit or containing spaces must raise ValueError."""
        with pytest.raises(ValueError, match="Invalid framework_name"):
            generate_adapter("123invalid", tmp_path)

    def test_empty_framework_name_raises_value_error(self, tmp_path: Path) -> None:
        """An empty framework name must raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            generate_adapter("", tmp_path)

    def test_output_directory_created_if_not_exists(self, tmp_path: Path) -> None:
        """generate_adapter() must create the output directory tree if missing."""
        new_dir = tmp_path / "deep" / "nested" / "dir"
        assert not new_dir.exists()
        generate_adapter("fw", new_dir)
        assert (new_dir / "adapters" / "fw.py").exists()
        assert (new_dir / "tests" / "test_fw_adapter.py").exists()

    def test_overwrite_protection_raises_file_exists_error(
        self, tmp_path: Path
    ) -> None:
        """Calling generate_adapter() twice for the same name must raise FileExistsError."""
        generate_adapter("dupfw", tmp_path)
        with pytest.raises(FileExistsError):
            generate_adapter("dupfw", tmp_path)


# ---------------------------------------------------------------------------
# Template content checks
# ---------------------------------------------------------------------------


class TestGeneratedContent:
    @pytest.mark.parametrize(
        "expected_fragment",
        [
            "def check_and_halt(",
            "def capabilities(",
            "def record_decision(",
            "def record_tokens(",
            "from veronica_core.adapter_capabilities import AdapterCapabilities",
            "_SUPPORTED_VERSIONS",
        ],
    )
    def test_generated_adapter_has_required_content(
        self, tmp_path: Path, expected_fragment: str
    ) -> None:
        """Generated adapter source must contain all required methods and imports."""
        generate_adapter("contentfw", tmp_path)
        src = (tmp_path / "adapters" / "contentfw.py").read_text()
        assert expected_fragment in src


# ---------------------------------------------------------------------------
# Naming edge cases
# ---------------------------------------------------------------------------


class TestNamingEdgeCases:
    def test_kebab_case_name_normalised(self, tmp_path: Path) -> None:
        """Hyphens in the name must be replaced by underscores in the file."""
        generate_adapter("my-framework", tmp_path)
        adapter_path = tmp_path / "adapters" / "my_framework.py"
        assert adapter_path.exists()

    def test_kebab_case_produces_pascal_class_name(self, tmp_path: Path) -> None:
        generate_adapter("my-framework", tmp_path)
        src = (tmp_path / "adapters" / "my_framework.py").read_text()
        assert "class MyFrameworkAdapter:" in src

    def test_name_with_numbers_accepted(self, tmp_path: Path) -> None:
        """Names like 'langchain2' (letter-first) are valid."""
        paths = generate_adapter("langchain2", tmp_path)
        assert all(p.exists() for p in paths)

    def test_name_with_special_chars_rejected(self, tmp_path: Path) -> None:
        """Names with spaces or special characters must raise ValueError."""
        with pytest.raises(ValueError):
            generate_adapter("my framework!", tmp_path)

    def test_uppercase_name_rejected(self, tmp_path: Path) -> None:
        """Uppercase names must raise ValueError to prevent PascalCase mismatch."""
        with pytest.raises(ValueError, match="Invalid framework_name"):
            generate_adapter("MyFramework", tmp_path)


# ---------------------------------------------------------------------------
# _to_pascal_case helper
# ---------------------------------------------------------------------------


class TestToPascalCase:
    @pytest.mark.parametrize(
        "input_name, expected",
        [
            ("myframework", "Myframework"),
            ("my_framework", "MyFramework"),
            ("my-framework", "MyFramework"),
            ("my_long_name", "MyLongName"),
            ("langchain2", "Langchain2"),
            ("a", "A"),
        ],
    )
    def test_pascal_case_conversion(self, input_name: str, expected: str) -> None:
        assert _to_pascal_case(input_name) == expected
