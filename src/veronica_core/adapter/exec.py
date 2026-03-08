"""Backward-compat shim for veronica_core.adapter.exec.

.. deprecated::
    Import from ``veronica_core.adapters.exec`` instead.
    This shim will be removed in a future major version.

Note: The DeprecationWarning is emitted by ``adapter/__init__.py``
when the ``veronica_core.adapter`` package is first imported.
No additional warning is emitted here to avoid double-firing.
"""

from veronica_core.adapters.exec import (  # noqa: F401
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
