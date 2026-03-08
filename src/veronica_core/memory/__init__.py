"""Memory Governance ABI for VERONICA Core.

Exports the complete public surface for memory governance:
- Type vocabulary (MemoryAction, MemoryProvenance, GovernanceVerdict)
- Operation and context dataclasses
- Hook protocol and built-in implementations
- MemoryGovernor orchestrator
"""

from __future__ import annotations

from veronica_core.memory.governor import MemoryGovernor
from veronica_core.memory.hooks import (
    DefaultMemoryGovernanceHook,
    DenyAllMemoryGovernanceHook,
    MemoryGovernanceHook,
)
from veronica_core.memory.types import (
    GovernanceVerdict,
    MemoryAction,
    MemoryGovernanceDecision,
    MemoryOperation,
    MemoryPolicyContext,
    MemoryProvenance,
)

__all__ = [
    "MemoryAction",
    "MemoryProvenance",
    "MemoryOperation",
    "MemoryPolicyContext",
    "GovernanceVerdict",
    "MemoryGovernanceDecision",
    "MemoryGovernanceHook",
    "DefaultMemoryGovernanceHook",
    "DenyAllMemoryGovernanceHook",
    "MemoryGovernor",
]
