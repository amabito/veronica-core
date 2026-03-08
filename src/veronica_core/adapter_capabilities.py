"""Capability declarations for framework adapters.

Each adapter declares its capabilities via AdapterCapabilities so that
orchestrators can discover adapter features at runtime without instantiation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Sentinel for adapters that have not declared a real version range yet.
UNCONSTRAINED_VERSIONS: tuple[str, str] = ("0.0.0", "99.99.99")


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for comparison.

    Non-numeric segments are treated as 0. Returns (0,) on empty input.

    Examples::

        _parse_version("0.4.0")  # (0, 4, 0)
        _parse_version("1.2")    # (1, 2)
        _parse_version("")       # (0,)
    """
    if not version:
        return (0,)
    parts = []
    for segment in version.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _compare_versions(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two parsed version tuples with implicit zero-padding.

    Returns negative if a < b, zero if a == b, positive if a > b.

    Without padding, Python tuple comparison treats (0, 4) < (0, 4, 0)
    because the shorter tuple is exhausted first and is considered "less".
    This function pads both tuples to the same length with trailing zeros
    so that "0.4" and "0.4.0" compare as equal.
    """
    max_len = max(len(a), len(b))
    a_padded = a + (0,) * (max_len - len(a))
    b_padded = b + (0,) * (max_len - len(b))
    if a_padded < b_padded:
        return -1
    if a_padded > b_padded:
        return 1
    return 0


@dataclass(frozen=True)
class AdapterCapabilities:
    """Static capability descriptor for a framework adapter.

    All fields default to False/empty so that new capabilities can be added
    without breaking existing adapters.

    Attributes:
        supports_streaming: Adapter can handle streaming LLM responses.
        supports_cost_extraction: Adapter can extract USD cost from responses.
        supports_token_extraction: Adapter can extract token counts.
        supports_async: Adapter provides async wrappers.
        supports_reserve_commit: Adapter uses two-phase budget reservation.
        supports_agent_identity: Adapter propagates A2A agent identity.
        framework_name: Human-readable framework name (e.g. "LangChain").
        framework_version_constraint: Optional version constraint string.
        supported_versions: Inclusive (min_version, max_version) range this
            adapter is tested against, e.g. ("0.4.0", "0.6.99").
            Defaults to ("0.0.0", "99.99.99") for backward compatibility.
        extra: Arbitrary extension metadata.
    """

    supports_streaming: bool = False
    supports_cost_extraction: bool = False
    supports_token_extraction: bool = False
    supports_async: bool = False
    supports_reserve_commit: bool = False
    supports_agent_identity: bool = False
    framework_name: str = ""
    framework_version_constraint: str = ""
    supported_versions: tuple[str, str] = UNCONSTRAINED_VERSIONS
    extra: dict[str, object] = field(default_factory=dict)

    def is_version_compatible(self, version: str) -> bool:
        """Return True if *version* falls within the supported_versions range.

        Comparison is performed on dotted integer tuples (e.g. "0.4.0" ->
        (0, 4, 0)).  Non-numeric segments are treated as 0.

        Args:
            version: The framework version string to check, e.g. "0.5.1".

        Returns:
            True if min_version <= version <= max_version, False otherwise.

        Examples::

            caps = AdapterCapabilities(supported_versions=("0.4.0", "0.6.99"))
            caps.is_version_compatible("0.5.0")   # True
            caps.is_version_compatible("0.3.99")  # False
            caps.is_version_compatible("0.7.0")   # False
        """
        min_ver, max_ver = self.supported_versions
        parsed = _parse_version(version)
        return (
            _compare_versions(_parse_version(min_ver), parsed) <= 0
            and _compare_versions(parsed, _parse_version(max_ver)) <= 0
        )
