"""VERONICA Container - Composite AI safety container."""

import warnings

from veronica_core.container.aicontainer import AIContainer

__all__ = ["AIContainer", "AIcontainer"]


def __getattr__(name: str):  # type: ignore[return]
    """Emit DeprecationWarning for the old AIcontainer name on attribute access."""
    if name == "AIcontainer":
        warnings.warn(
            "AIcontainer is deprecated and will be removed in a future release. "
            "Use AIContainer instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return AIContainer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
