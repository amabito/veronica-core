"""VERONICA Security Containment Layer."""
from veronica_core.security.capabilities import Capability, CapabilitySet, has_cap
from veronica_core.security.ci_guard import CIGuard, Finding
from veronica_core.security.key_providers import (
    EnvKeyProvider,
    FileKeyProvider,
    KeyProvider,
    VaultKeyProvider,
)
from veronica_core.security.masking import SecretMasker
from veronica_core.security.policy_engine import (
    ExecPolicyContext,
    ExecPolicyDecision,
    PolicyContext,  # backward-compatible alias for ExecPolicyContext
    PolicyDecision,  # backward-compatible alias for ExecPolicyDecision
    PolicyEngine,
    PolicyHook,
)

__all__ = [
    "Capability",
    "CapabilitySet",
    "has_cap",
    "CIGuard",
    "Finding",
    "EnvKeyProvider",
    "FileKeyProvider",
    "KeyProvider",
    "VaultKeyProvider",
    "SecretMasker",
    "ExecPolicyContext",
    "ExecPolicyDecision",
    "PolicyContext",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyHook",
]
