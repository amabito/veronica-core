"""VERONICA kernel package -- core governance primitives.

Exports:
- DecisionEnvelope: attestation wrapper for governance decisions (opt-in per path)
- ReasonCode: machine-readable reason codes
- make_envelope: factory for DecisionEnvelope with auto-generated audit fields
"""

from veronica_core.kernel.decision import DecisionEnvelope, ReasonCode, make_envelope

__all__ = [
    "DecisionEnvelope",
    "ReasonCode",
    "make_envelope",
]
