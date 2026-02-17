"""VERONICA Core - Failsafe state machine for mission-critical applications."""

__version__ = "0.2.0"

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

# Runtime Policy Control (v0.2)
from veronica_core.runtime_policy import (
    RuntimePolicy,
    PolicyContext,
    PolicyDecision,
    PolicyPipeline,
)

# LLM safety modules
# BudgetEnforcer, AgentStepGuard, RetryContainer, CircuitBreaker implement RuntimePolicy
# PartialResultBuffer is a data preservation utility (not a policy primitive)
from veronica_core.budget import BudgetEnforcer
from veronica_core.agent_guard import AgentStepGuard
from veronica_core.partial import PartialResultBuffer
from veronica_core.retry import RetryContainer
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState

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
    # Runtime Policy Control (v0.2)
    "RuntimePolicy",
    "PolicyContext",
    "PolicyDecision",
    "PolicyPipeline",
    # LLM Safety (implement RuntimePolicy)
    "BudgetEnforcer",
    "AgentStepGuard",
    "PartialResultBuffer",
    "RetryContainer",
    "CircuitBreaker",
    "CircuitState",
    # Integration
    "VeronicaIntegration",
    "get_veronica_integration",
]
