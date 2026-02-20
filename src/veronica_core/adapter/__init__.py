"""VERONICA Secure Adapter — policy-checked execution layer."""
from veronica_core.adapter.exec import (
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
