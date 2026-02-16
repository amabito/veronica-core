"""VERONICA Core - Failsafe state machine for mission-critical applications."""

__version__ = "0.1.0"

# Core state machine
from veronica_core.state import (
    VeronicaState,
    StateTransition,
    VeronicaStateMachine,
)

# Persistence backends
from veronica_core.backends import (
    PersistenceBackend,
    JSONBackend,
    MemoryBackend,
)
from veronica_core.persist import VeronicaPersistence  # Deprecated, use JSONBackend

# Exit handling
from veronica_core.exit import (
    ExitTier,
    VeronicaExit,
)

# Guard interface
from veronica_core.guards import (
    VeronicaGuard,
    PermissiveGuard,
)

# LLM client interface (optional)
from veronica_core.clients import (
    LLMClient,
    NullClient,
    DummyClient,
)

# Integration API (main entry point)
from veronica_core.integration import (
    VeronicaIntegration,
    get_veronica_integration,
)

__all__ = [
    # Core
    "VeronicaState",
    "StateTransition",
    "VeronicaStateMachine",
    # Backends
    "PersistenceBackend",
    "JSONBackend",
    "MemoryBackend",
    "VeronicaPersistence",  # Deprecated
    # Exit
    "ExitTier",
    "VeronicaExit",
    # Guards
    "VeronicaGuard",
    "PermissiveGuard",
    # LLM Clients (optional)
    "LLMClient",
    "NullClient",
    "DummyClient",
    # Integration
    "VeronicaIntegration",
    "get_veronica_integration",
]
