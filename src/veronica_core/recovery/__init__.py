"""VERONICA self-healing containment layer.

Exports all public types from the recovery subpackage.
"""

from __future__ import annotations

from veronica_core.recovery.checkpoint import (
    CheckpointManager,
    ContainmentCheckpoint,
    RestoreResult,
)
from veronica_core.recovery.integrity import (
    IntegrityMonitor,
    IntegrityVerdict,
)
from veronica_core.recovery.orchestrator import (
    RecoveryAction,
    RecoveryOrchestrator,
)
from veronica_core.recovery.sentinel import (
    HeartbeatProtocol,
    HeartbeatVerdict,
    SentinelMonitor,
    SignedHeartbeat,
)

__all__ = [
    "CheckpointManager",
    "ContainmentCheckpoint",
    "RestoreResult",
    "IntegrityMonitor",
    "IntegrityVerdict",
    "RecoveryAction",
    "RecoveryOrchestrator",
    "HeartbeatProtocol",
    "HeartbeatVerdict",
    "SentinelMonitor",
    "SignedHeartbeat",
]
