"""VERONICA Secure Adapter -- backward-compat re-export shim.

.. deprecated::
    ``veronica_core.adapter`` is deprecated. Use ``veronica_core.adapters`` instead.
    This shim will be removed in a future major version.
"""

import warnings

warnings.warn(
    "veronica_core.adapter is deprecated; use veronica_core.adapters instead.",
    DeprecationWarning,
    stacklevel=2,
)

from veronica_core.adapters.exec import (  # noqa: E402
    AdapterConfig,
    ApprovalRequiredError,
    SecureExecutor,
    SecurePermissionError,
)

__all__ = [
    "AdapterConfig",
    "ApprovalRequiredError",
    "SecureExecutor",
    "SecurePermissionError",
]
