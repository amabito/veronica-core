"""Policy bundle verifier for VERONICA Core (v3.2+).

Performs startup-time validation of a PolicyBundle before it is allowed
into the live execution path.  The verifier is fail-closed: a single
error marks the bundle invalid and prevents it from being used.

No external dependencies are required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from veronica_core.policy.bundle import PolicyBundle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of verifying a PolicyBundle.

    Attributes:
        valid: True only if all required checks passed with zero errors.
        errors: Tuple of error messages (non-empty implies invalid).
        warnings: Tuple of advisory messages (do not affect validity).
        verified_at: Unix timestamp when verification was performed.
    """

    valid: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    verified_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

_KNOWN_RULE_TYPES: frozenset[str] = frozenset(
    {
        "budget",
        "step",
        "retry",
        "timeout",
        "shell",
        "network",
        "file",
        "git",
        "memory",
        "trust",
        "custom",
    }
)


class PolicyVerifier:
    """Validates a PolicyBundle before it enters the execution path.

    Checks performed (in order):
        1. Content hash matches computed hash (if metadata.content_hash is set).
        2. Epoch is non-negative (structural validation -- caught earlier by
           PolicyMetadata, but re-checked here for defence in depth).
        3. All rule_types are in the allowed set.
        4. No duplicate rule_ids within the bundle.
        5. Signature required but missing.
        6. Signature verification (if signer provides a verify() method).

    Args:
        allowed_rule_types: Frozenset of permitted rule type strings.
                            Defaults to KNOWN_RULE_TYPES.
        require_signature: If True, bundles without a signature are rejected.
        signer: Optional object with a ``verify_bundle(bundle) -> bool``
                method.  If provided and *require_signature* is True (or the
                bundle is signed), the signature is verified via this signer.
    """

    KNOWN_RULE_TYPES: frozenset[str] = _KNOWN_RULE_TYPES

    def __init__(
        self,
        allowed_rule_types: frozenset[str] | None = None,
        require_signature: bool = False,
        signer: Any = None,
    ) -> None:
        self._allowed = (
            allowed_rule_types if allowed_rule_types is not None else _KNOWN_RULE_TYPES
        )
        self._require_signature = require_signature
        self._signer = signer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self, bundle: PolicyBundle) -> VerificationResult:
        """Validate *bundle* and return a VerificationResult.

        Fail-closed: any error produces ``valid=False``.

        Args:
            bundle: The PolicyBundle to validate.

        Returns:
            VerificationResult with valid=True if all checks pass.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # --- Check 1: Content hash (if metadata declares one) ---
        declared_hash = bundle.metadata.content_hash
        if declared_hash:
            computed = bundle.content_hash()
            if not bundle.verify_content_hash():
                errors.append(
                    f"Content hash mismatch: declared={declared_hash!r}, "
                    f"computed={computed!r}"
                )
        else:
            warnings.append(
                "PolicyBundle has no declared content_hash; "
                "tamper detection is disabled"
            )

        # --- Check 2: Epoch non-negative (defence in depth) ---
        if bundle.metadata.epoch < 0:
            errors.append(
                f"PolicyBundle.metadata.epoch must be non-negative, "
                f"got {bundle.metadata.epoch}"
            )

        # --- Check 3: All rule_types in allowed set ---
        for rule in bundle.rules:
            if rule.rule_type not in self._allowed:
                errors.append(
                    f"Rule {rule.rule_id!r} has unknown rule_type={rule.rule_type!r}; "
                    f"allowed types: {sorted(self._allowed)}"
                )

        # --- Check 4: No duplicate rule_ids ---
        seen_ids: set[str] = set()
        for rule in bundle.rules:
            if rule.rule_id in seen_ids:
                errors.append(f"Duplicate rule_id={rule.rule_id!r} in bundle")
            seen_ids.add(rule.rule_id)

        # --- Check 5: Signature required but missing ---
        if self._require_signature and not bundle.is_signed:
            errors.append(
                "Bundle signature is required (require_signature=True) "
                "but the bundle is unsigned"
            )

        # --- Check 5b: Signer provided but bundle unsigned (advisory) ---
        if (
            self._signer is not None
            and not bundle.is_signed
            and not self._require_signature
        ):
            warnings.append(
                "Signer was provided but bundle is unsigned and "
                "require_signature=False; signature verification was skipped"
            )

        # --- Check 6a: Signed bundle but no signer (fail-closed) ---
        if bundle.is_signed and self._signer is None:
            errors.append(
                "Bundle is signed but no signer was provided to verify "
                "the signature (fail-closed)"
            )

        # --- Check 6b: Signature verification ---
        if bundle.is_signed and self._signer is not None:
            verify_fn = getattr(self._signer, "verify_bundle", None)
            if callable(verify_fn):
                try:
                    if not verify_fn(bundle):
                        errors.append("Bundle signature verification failed")
                except Exception as exc:
                    logger.debug(
                        "Bundle signature verification raised %s: %s",
                        type(exc).__name__, exc,
                    )
                    errors.append("Bundle signature verification failed")
            else:
                errors.append(
                    "Signer was provided but has no verify_bundle() method; "
                    "signature cannot be verified (fail-closed)"
                )

        valid = len(errors) == 0
        return VerificationResult(
            valid=valid,
            errors=tuple(errors),
            warnings=tuple(warnings),
            verified_at=time.time(),
        )

    @classmethod
    def verify_or_halt(
        cls,
        bundle: "PolicyBundle",
        signer: Any = None,
    ) -> "VerificationResult":
        """Production startup entry point -- fail-closed on any error.

        Constructs a verifier with ``require_signature=True`` and verifies
        the bundle.  Any exception raised during verification is caught and
        returned as a failed VerificationResult rather than propagating.

        When ``signer`` is None, the result is always invalid because
        signature verification cannot be performed (fail-closed).

        Args:
            bundle: The PolicyBundle to validate.
            signer: Signer with ``verify_bundle(bundle) -> bool``.
                    When None, verification always fails (fail-closed).

        Returns:
            VerificationResult with valid=True only if all checks pass
            including signature verification.  Always valid=False on error.
        """
        # C2: fail-closed -- cannot verify signature without a signer.
        if signer is None:
            return VerificationResult(
                valid=False,
                errors=(
                    "verify_or_halt requires a signer for signature verification "
                    "(fail-closed: signer=None is not allowed)",
                ),
            )
        verifier = cls(require_signature=True, signer=signer)
        try:
            return verifier.verify(bundle)
        except Exception as exc:
            logger.debug(
                "[verifier] verify_or_halt raised %s: %s", type(exc).__name__, exc
            )
            return VerificationResult(
                valid=False,
                errors=("Verification failed",),
            )
