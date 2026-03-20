"""Tests for veronica_core.security.rollback_guard -- parse_version() and RollbackGuard."""

from __future__ import annotations


from veronica_core.security.rollback_guard import parse_version


# ---------------------------------------------------------------------------
# T6: parse_version() invalid / edge inputs
# ---------------------------------------------------------------------------


class TestParseVersionValid:
    """parse_version() correctly handles well-formed version strings."""

    def test_simple_dotted(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_two_component(self) -> None:
        assert parse_version("0.9") == (0, 9)

    def test_single_component(self) -> None:
        assert parse_version("5") == (5,)

    def test_hyphen_separator(self) -> None:
        """Hyphenated semver pre-release: numeric parts extracted in order."""
        result = parse_version("1.0.0-alpha")
        # "alpha" is not a digit -- parse_version drops it
        assert result == (1, 0, 0)

    def test_leading_v_prefix(self) -> None:
        """parse_version('v1.0.0') -- 'v' is not a digit, first part dropped."""
        result = parse_version("v1.0.0")
        # "v1" -> re.split -> ["v1", "0", "0"] but "v1".isdigit() is False
        # Only the two "0" parts are kept.
        assert result == (0, 0)

    def test_v_prefix_alternative(self) -> None:
        """Confirm v1.0.0 yields tuple with exactly 2 zeros (not 3)."""
        result = parse_version("v1.0.0")
        assert len(result) == 2


class TestParseVersionInvalid:
    """parse_version() handles malformed inputs without raising."""

    def test_empty_string_returns_empty_tuple(self) -> None:
        """parse_version('') must return an empty tuple -- no ints extractable."""
        result = parse_version("")
        assert result == ()

    def test_all_alpha_returns_empty_tuple(self) -> None:
        """parse_version('abc') must return an empty tuple."""
        result = parse_version("abc")
        assert result == ()

    def test_double_dot_drops_empty_segment(self) -> None:
        """parse_version('1..2') -- empty segment between dots is not a digit,
        so at most (1, 2) or a subset depending on slice [:3]."""
        result = parse_version("1..2")
        # re.split(r"[.-]", "1..2") -> ["1", "", "2"]; "" is not a digit -> dropped
        # Only 1 and 2 survive.
        assert 1 in result
        assert 2 in result

    def test_only_dots_returns_empty_tuple(self) -> None:
        """parse_version('...') must return an empty tuple."""
        result = parse_version("...")
        assert result == ()

    def test_non_numeric_returns_empty_tuple(self) -> None:
        """parse_version('abc.def.ghi') must return an empty tuple."""
        assert parse_version("abc.def.ghi") == ()

    def test_large_version_capped_at_three(self) -> None:
        """Only the first 3 numeric components are returned."""
        result = parse_version("1.2.3.4.5")
        assert len(result) <= 3
        assert result[:3] == (1, 2, 3)

    def test_return_type_is_tuple(self) -> None:
        """parse_version() always returns a tuple (not a list)."""
        assert isinstance(parse_version("1.2.3"), tuple)
        assert isinstance(parse_version(""), tuple)
        assert isinstance(parse_version("abc"), tuple)
