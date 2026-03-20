"""Tests for veronica_core._utils validation utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from veronica_core._utils import check_path_within_root, require_strict_int


# ---------------------------------------------------------------------------
# require_strict_int
# ---------------------------------------------------------------------------


class TestRequireStrictInt:
    def test_valid_positive_int(self) -> None:
        require_strict_int(5, "x")

    def test_zero_with_default_min(self) -> None:
        require_strict_int(0, "x")  # min_value=0 default

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="x must be >= 0"):
            require_strict_int(-1, "x")

    def test_bool_true_raises(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            require_strict_int(True, "x")

    def test_bool_false_raises(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            require_strict_int(False, "x")

    def test_float_raises(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            require_strict_int(3.0, "x")

    def test_string_raises(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            require_strict_int("5", "x")

    def test_none_raises(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            require_strict_int(None, "x")

    def test_min_value_none_skips_range(self) -> None:
        require_strict_int(-100, "x", min_value=None)  # no range check

    def test_custom_min_value(self) -> None:
        require_strict_int(5, "x", min_value=5)  # boundary: equal
        with pytest.raises(ValueError, match="x must be >= 5"):
            require_strict_int(4, "x", min_value=5)


# ---------------------------------------------------------------------------
# check_path_within_root
# ---------------------------------------------------------------------------


class TestCheckPathWithinRoot:
    def test_path_within_root(self, tmp_path: Path) -> None:
        child = tmp_path / "a" / "b.json"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.touch()
        result = check_path_within_root(child, tmp_path)
        assert result.is_absolute()

    def test_path_outside_root_raises(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.json"
        outside.touch()
        with pytest.raises(ValueError, match="Path traversal denied"):
            check_path_within_root(outside, tmp_path)

    def test_dotdot_traversal_raises(self, tmp_path: Path) -> None:
        subdir = tmp_path / "sub"
        subdir.mkdir()
        target = tmp_path / "secret.json"
        target.touch()
        with pytest.raises(ValueError, match="Path traversal denied"):
            check_path_within_root(subdir / ".." / "secret.json", subdir)

    def test_root_equals_path(self, tmp_path: Path) -> None:
        """Path equal to root should be allowed."""
        result = check_path_within_root(tmp_path, tmp_path)
        assert result == tmp_path.resolve()

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        child = tmp_path / "test.json"
        child.touch()
        result = check_path_within_root(str(child), tmp_path)
        assert result.is_absolute()

    def test_error_message_no_path_leak(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "secret.json"
        outside.touch()
        with pytest.raises(ValueError) as exc_info:
            check_path_within_root(outside, tmp_path)
        msg = str(exc_info.value)
        assert str(tmp_path) not in msg
        assert str(outside) not in msg
