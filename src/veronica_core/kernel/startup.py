"""Startup guard for signed policy bundles.

Verifies a PolicyBundle at startup using a signer and emits a governance
audit event when verification fails and an audit_log is provided.

No reasoning, no AI, no sandbox, no policy authoring is implemented here.
This module is pure deterministic wiring between the policy verifier and the
audit log.
"""

from __future__ import annotations

__all__ = [
    "load_and_verify",
    "verify_policy_or_halt",
]

import logging
from typing import TYPE_CHECKING, Any

from veronica_core.kernel.decision import ReasonCode, make_envelope
from veronica_core.policy.bundle import PolicyBundle, PolicyMetadata, PolicyRule
from veronica_core.policy.verifier import PolicyVerifier, VerificationResult

if TYPE_CHECKING:
    from veronica_core.audit.log import AuditLog

logger = logging.getLogger(__name__)


def verify_policy_or_halt(
    bundle: PolicyBundle,
    signer: Any,
    audit_log: "AuditLog | None" = None,
) -> VerificationResult:
    """Verify a PolicyBundle and halt if invalid.

    Calls PolicyVerifier.verify_or_halt(bundle, signer).  When verification
    fails and audit_log is provided, emits a governance audit event describing
    the failure reason (POLICY_UNSIGNED or POLICY_HASH_MISMATCH).

    Args:
        bundle: The PolicyBundle to verify.
        signer: Signer with ``verify_bundle(bundle) -> bool`` method.
                When None, verification always fails (fail-closed).
        audit_log: Optional AuditLog for governance event emission on failure.
                   When None, no audit event is written (no crash).

    Returns:
        VerificationResult with valid=True only if all checks pass.
        Always valid=False when signer is None or any check fails.
    """
    result = PolicyVerifier.verify_or_halt(bundle, signer)

    if not result.valid and audit_log is not None:
        # Determine the most specific reason code from the error messages.
        errors_lower = " ".join(result.errors).lower()
        if "unsigned" in errors_lower or "signature is required" in errors_lower:
            reason_code = ReasonCode.POLICY_UNSIGNED
        else:
            reason_code = ReasonCode.POLICY_HASH_MISMATCH

        reason_text = "; ".join(result.errors) if result.errors else "Policy verification failed"

        envelope = make_envelope(
            decision="HALT",
            reason_code=reason_code,
            reason=reason_text,
            issuer="StartupGuard",
            policy_hash=bundle.metadata.content_hash,
            policy_epoch=bundle.metadata.epoch,
        )

        audit_log.write_governance_event(
            event_type="GOVERNANCE_HALT",
            decision=envelope.decision,
            reason_code=envelope.reason_code,
            reason=envelope.reason,
            audit_id=envelope.audit_id,
            policy_hash=envelope.policy_hash,
            policy_epoch=envelope.policy_epoch,
            issuer=envelope.issuer,
        )

    return result


def load_and_verify(
    bundle_data: dict[str, Any],
    signer: Any,
    audit_log: "AuditLog | None" = None,
) -> tuple[PolicyBundle, VerificationResult]:
    """Load a PolicyBundle from a raw dict and verify it.

    Constructs a PolicyBundle from *bundle_data*, then delegates to
    verify_policy_or_halt().  Fail-closed: any exception during construction
    produces a VerificationResult with valid=False rather than propagating.

    Args:
        bundle_data: Raw dict with keys ``metadata`` (dict) and ``rules``
                     (list of dicts).  Unknown keys are ignored.
        signer: Signer for signature verification passed to verify_policy_or_halt.
        audit_log: Optional AuditLog for governance event emission on failure.

    Returns:
        (bundle, result) tuple.  When construction fails, bundle is a
        minimal placeholder and result has valid=False.
    """
    try:
        meta_data: dict[str, Any] = bundle_data.get("metadata", {})
        metadata = PolicyMetadata(
            policy_id=meta_data["policy_id"],
            version=meta_data.get("version", "1.0.0"),
            epoch=meta_data.get("epoch", 0),
            issuer=meta_data.get("issuer", ""),
            description=meta_data.get("description", ""),
            tags=meta_data.get("tags", {}),
            content_hash=meta_data.get("content_hash", ""),
        )

        rules: tuple[PolicyRule, ...] = tuple(
            PolicyRule(
                rule_id=r["rule_id"],
                rule_type=r["rule_type"],
                parameters=r.get("parameters", {}),
                enabled=r.get("enabled", True),
                priority=r.get("priority", 100),
            )
            for r in bundle_data.get("rules", [])
        )

        bundle = PolicyBundle(
            metadata=metadata,
            rules=rules,
            signature=bundle_data.get("signature", ""),
        )

    except Exception as exc:
        # Fail-closed: construction failure means the bundle is untrusted.
        logger.debug(
            "[startup] bundle construction raised %s: %s", type(exc).__name__, exc
        )
        bad_bundle = PolicyBundle(
            metadata=PolicyMetadata(policy_id="__invalid__"),
            rules=(),
        )
        result = VerificationResult(
            valid=False,
            errors=("Bundle construction failed",),
        )
        return bad_bundle, result

    result = verify_policy_or_halt(bundle, signer, audit_log=audit_log)
    return bundle, result
