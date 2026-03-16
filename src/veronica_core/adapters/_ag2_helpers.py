"""Shared helpers for AG2 adapter modules.

Internal module -- not part of the public API.
"""

from __future__ import annotations

# Supported AG2/AutoGen version range -- tested and validated.
# Both ag2.py (VeronicaConversableAgent) and ag2_capability.py
# (CircuitBreakerCapability) report this same range via capabilities().
_AG2_SUPPORTED_VERSIONS: tuple[str, str] = ("0.4.0", "0.6.99")


def emit_ag2_otel_event(
    agent_name: str, decision: str, reason: str, check_type: str
) -> None:
    """Add a veronica containment event to the current OTel span.

    No-op if OTel is not enabled. Never raises.
    """
    try:
        from veronica_core.otel import emit_containment_decision

        emit_containment_decision(
            decision_name=decision,
            reason=f"[{check_type}] {agent_name}: {reason}",
        )
    except Exception:
        # Intentionally swallowed: this helper is declared "Never raises";
        # OTel emission is best-effort telemetry that must not disrupt agent
        # containment logic.
        pass
