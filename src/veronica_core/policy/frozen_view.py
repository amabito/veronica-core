"""Immutable runtime policy view and thread-safe holder for VERONICA Core (v3.2+).

FrozenPolicyView wraps a verified PolicyBundle into a read-only runtime
object with convenient query helpers.

PolicyViewHolder provides an atomic swap mechanism so the live execution
path always sees a consistent view while hot-reload is in progress.

No external dependencies are required.
"""

from __future__ import annotations

import threading
from typing import Any

from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.verifier import PolicyVerifier, VerificationResult


class FrozenPolicyView:
    """Read-only runtime wrapper around a verified PolicyBundle.

    Construction raises ValueError if the supplied VerificationResult
    is invalid (fail-closed semantics -- an invalid bundle must never
    reach the execution path).

    Args:
        bundle: The PolicyBundle to wrap.
        verification: Result of verifying *bundle*.

    Raises:
        ValueError: If verification.valid is False.
    """

    __slots__ = (
        "_bundle",
        "_verification",
        "_rule_types",
    )

    def __init__(
        self,
        bundle: PolicyBundle,
        verification: VerificationResult,
    ) -> None:
        if not verification.valid:
            raise ValueError(
                f"Cannot create FrozenPolicyView from an invalid bundle: "
                f"{'; '.join(verification.errors)}"
            )
        self._bundle = bundle
        self._verification = verification
        self._rule_types: frozenset[str] = frozenset(
            r.rule_type for r in bundle.rules
        )

    # ------------------------------------------------------------------
    # Core properties
    # ------------------------------------------------------------------

    @property
    def metadata(self) -> PolicyMetadata:
        """Return the bundle's immutable metadata."""
        return self._bundle.metadata

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        """Return all rules (including disabled ones) from the bundle."""
        return self._bundle.rules

    @property
    def verification(self) -> VerificationResult:
        """Return the VerificationResult used to create this view."""
        return self._verification

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def rules_for_type(self, rule_type: str) -> tuple[PolicyRule, ...]:
        """Return all rules whose rule_type matches *rule_type*.

        Returns an empty tuple if no rules of that type exist.
        Note: includes disabled rules -- use active_rules on the bundle
        if you want only enabled rules.
        """
        return tuple(r for r in self._bundle.rules if r.rule_type == rule_type)

    def has_rule_type(self, rule_type: str) -> bool:
        """Return True if at least one rule of *rule_type* exists (any enabled state)."""
        return rule_type in self._rule_types

    @property
    def rule_types(self) -> frozenset[str]:
        """Return the frozenset of distinct rule_type values in the bundle."""
        return self._rule_types

    # ------------------------------------------------------------------
    # Audit integration
    # ------------------------------------------------------------------

    def to_audit_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for inclusion in an audit entry.

        The dict contains enough information to identify the policy at audit
        review time without reproducing the full rule parameters (which may
        be large or sensitive).
        """
        meta = self._bundle.metadata
        return {
            "policy_id": meta.policy_id,
            "version": meta.version,
            "epoch": meta.epoch,
            "issuer": meta.issuer,
            "content_hash": meta.content_hash,
            "is_signed": self._bundle.is_signed,
            "rule_count": len(self._bundle.rules),
            "rule_types": sorted(self._rule_types),
            "verified_at": self._verification.verified_at,
        }


# ---------------------------------------------------------------------------
# Thread-safe holder
# ---------------------------------------------------------------------------


class PolicyViewHolder:
    """Thread-safe container for the active FrozenPolicyView.

    Supports atomic swap so that callers always observe a consistent
    view even when a reload is happening concurrently.

    Args:
        initial: Optional initial FrozenPolicyView (may be None).
    """

    def __init__(self, initial: FrozenPolicyView | None = None) -> None:
        self._lock = threading.Lock()
        self._view: FrozenPolicyView | None = initial

    @property
    def current(self) -> FrozenPolicyView | None:
        """Return the current view under the holder's lock."""
        with self._lock:
            return self._view

    def swap(self, new_view: FrozenPolicyView | None) -> FrozenPolicyView | None:
        """Atomically replace the current view with *new_view*.

        Args:
            new_view: The replacement view (may be None to clear).

        Returns:
            The previous view (may be None).
        """
        with self._lock:
            old = self._view
            self._view = new_view
            return old

    def load_bundle(
        self,
        bundle: PolicyBundle,
        verifier: PolicyVerifier | None = None,
    ) -> VerificationResult:
        """Verify *bundle* and, if valid, atomically install it as the current view.

        If verification fails the existing view is left unchanged (fail-closed).

        Args:
            bundle: The PolicyBundle to verify and install.
            verifier: Verifier to use.  If None a default PolicyVerifier() is used.

        Returns:
            The VerificationResult from verification.
        """
        v = verifier if verifier is not None else PolicyVerifier()
        result = v.verify(bundle)
        if result.valid:
            view = FrozenPolicyView(bundle, result)
            self.swap(view)
        return result
