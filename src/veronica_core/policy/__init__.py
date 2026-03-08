"""Declarative Policy Layer for VERONICA Execution Shield (v2.1+).

Provides YAML/JSON-based policy definition, loading, and the
policy-attested bundle foundation introduced in v3.2.
"""

from veronica_core.policy.schema import (
    PolicySchema,
    PolicyValidationError,
    RuleSchema,
)
from veronica_core.policy.registry import PolicyRegistry
from veronica_core.policy.loader import PolicyLoader, LoadedPolicy, WatchHandle

# Policy-attested bundle types (v3.2)
from veronica_core.policy.bundle import (
    PolicyBundle,
    PolicyMetadata,
    PolicyRule,
)
from veronica_core.policy.verifier import PolicyVerifier, VerificationResult
from veronica_core.policy.frozen_view import FrozenPolicyView, PolicyViewHolder
from veronica_core.policy.audit_helpers import enrich_audit_with_policy

__all__ = [
    # Existing declarative layer
    "PolicySchema",
    "RuleSchema",
    "PolicyRegistry",
    "PolicyLoader",
    "PolicyValidationError",
    "LoadedPolicy",
    "WatchHandle",
    # Policy-attested bundle foundation (v3.2)
    "PolicyBundle",
    "PolicyMetadata",
    "PolicyRule",
    "PolicyVerifier",
    "VerificationResult",
    "FrozenPolicyView",
    "PolicyViewHolder",
    "enrich_audit_with_policy",
]
