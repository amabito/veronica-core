"""VERONICA Core - Failsafe state machine for mission-critical applications."""

__version__ = "3.0.0"

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
from veronica_core.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    FailurePredicate,
    ignore_exception_types,
    count_exception_types,
    ignore_status_codes,
)

# Integration API (main entry point)
from veronica_core.integration import (
    VeronicaIntegration,
    get_veronica_integration,
)

# Execution Shield (v0.3 -- opt-in, all features disabled by default)
from veronica_core.shield import ShieldConfig, PostDispatchHook
from veronica_core.shield.config import (
    SafeModeConfig,
    BudgetConfig,
    CircuitBreakerConfig,
    EgressConfig,
    SecretGuardConfig,
    BudgetWindowConfig,
    TokenBudgetConfig,
    InputCompressionConfig,
    AdaptiveBudgetConfig,
    TimeAwarePolicyConfig,
)
from veronica_core.shield.budget_window import BudgetWindowHook
from veronica_core.shield.token_budget import TokenBudgetHook
from veronica_core.shield.input_compression import InputCompressionHook
from veronica_core.shield.adaptive_budget import AdaptiveBudgetHook, AdjustmentResult
from veronica_core.shield.time_policy import TimeAwarePolicy, TimeResult

# Runtime Policies (v0.4.3 -- opt-in, all features disabled by default)
from veronica_core.policies.minimal_response import MinimalResponsePolicy

# Execution Containment (v0.9.0)
from veronica_core.containment import (
    CancellationToken,
    ChainMetadata,
    ContextSnapshot,
    ExecutionConfig,
    ExecutionContext,
    ExecutionGraph,
    NodeEvent,
    NodeRecord,
    WrapOptions,
    get_current_partial_buffer,
    attach_partial_buffer,
    # BudgetAllocator (v1.6.0)
    AllocationResult,
    BudgetAllocator,
    FairShareAllocator,
    WeightedAllocator,
    DynamicAllocator,
)

# Execution boundary (v0.9.1)
from veronica_core.container import AIContainer

# Decorator-based injection (v0.9.3)
from veronica_core.inject import (
    veronica_guard,
    GuardConfig,
    VeronicaHalt,
    is_guard_active,
    get_active_container,
)

# SDK patch module (v0.9.4 -- opt-in, not applied on import)
from veronica_core.patch import patch_openai, patch_anthropic, unpatch_all

# Semantic Loop Guard (v0.9.6)
from veronica_core.semantic import SemanticLoopGuard

# Auto Pricing (v0.10.0)
from veronica_core.pricing import (
    estimate_cost_usd,
    resolve_model_pricing,
    Pricing,
    extract_usage_from_response,
)

# Distributed Budget (v0.10.0)
from veronica_core.distributed import (
    BudgetBackend,
    ReservableBudgetBackend,
    LocalBudgetBackend,
    RedisBudgetBackend,
    get_default_backend,
    CircuitSnapshot,
    DistributedCircuitBreaker,
    get_default_circuit_breaker,
)

# OpenTelemetry (v0.10.0)
from veronica_core.otel import (
    enable_otel,
    disable_otel,
    is_otel_enabled,
    enable_otel_with_provider,
    OTelExecutionGraphObserver,
)

# Degradation Ladder (v0.10.0)
from veronica_core.shield.degradation import (
    DegradationLadder,
    DegradationConfig,
    Trimmer,
    NoOpTrimmer,
)

# PolicyDecision helpers (v0.10.0)
from veronica_core.runtime_policy import (
    allow,
    deny,
    model_downgrade,
    rate_limit_decision,
)

# AG2 AgentCapability-compatible adapters (v0.11.0)
from veronica_core.adapters.ag2_capability import CircuitBreakerCapability

# MCP containment adapter (v1.6.0)
from veronica_core.adapters.mcp import (
    MCPContainmentAdapter,
    MCPToolCost,
    MCPToolResult,
    MCPToolStats,
)

# Async MCP containment (v1.7.0)
from veronica_core.adapters.mcp_async import (
    AsyncMCPContainmentAdapter,
    wrap_mcp_server,
)

# ASGI/WSGI Middleware (v0.11.0)
from veronica_core.middleware import (
    VeronicaASGIMiddleware,
    VeronicaWSGIMiddleware,
    get_current_execution_context,
)

# Compliance Export (v1.4.0)
from veronica_core.compliance import ComplianceExporter

# Audit Chain (v3.0.0)
from veronica_core.compliance.audit_chain import AuditChain, AuditEntry

# Quickstart API (v1.4.0 -- 2-line setup shortcut)
from veronica_core.quickstart import init, shutdown, get_context

# Protocol definitions (v1.6.0 -- integration contracts for external frameworks)
from veronica_core.protocols import (
    FrameworkAdapterProtocol,
    PlannerProtocol,
    ExecutionGraphObserver,
    ContainmentMetricsProtocol,
)

# Adapter capabilities (v3.0.0)
from veronica_core.adapter_capabilities import AdapterCapabilities

# Metrics implementations (v1.6.0)
from veronica_core.metrics import LoggingContainmentMetrics

# OTel Feedback Loop (v2.4)
from veronica_core.otel_feedback import (
    AgentMetrics,
    OTelMetricsIngester,
    MetricRule,
    MetricsDrivenPolicy,
)

# Policy Simulation (v2.6.0)
from veronica_core.simulation import (
    ExecutionLog,
    ExecutionLogEntry,
    PolicySimulator,
    SimulationEvent,
    SimulationReport,
)

# A2A Trust Boundary (v2.7)
from veronica_core.a2a import (
    TrustLevel,
    AgentIdentity,
    TrustPolicy,
    TrustBasedPolicyRouter,
    TrustEscalationTracker,
    identity_from_a2a_card,
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
    "FailurePredicate",
    "ignore_exception_types",
    "count_exception_types",
    "ignore_status_codes",
    # Integration
    "VeronicaIntegration",
    "get_veronica_integration",
    # Execution Shield (v0.3)
    "ShieldConfig",
    "PostDispatchHook",
    "SafeModeConfig",
    "BudgetConfig",
    "CircuitBreakerConfig",
    "EgressConfig",
    "SecretGuardConfig",
    # Execution Shield (v0.4)
    "BudgetWindowConfig",
    "BudgetWindowHook",
    "TokenBudgetConfig",
    "TokenBudgetHook",
    # Input Compression (v0.5.0)
    "InputCompressionConfig",
    "InputCompressionHook",
    # Adaptive Budget (v0.6.0)
    "AdaptiveBudgetConfig",
    "AdaptiveBudgetHook",
    "AdjustmentResult",
    # Time-Aware Policy (v0.6.0)
    "TimeAwarePolicyConfig",
    "TimeAwarePolicy",
    "TimeResult",
    # Runtime Policies (v0.4.3)
    "MinimalResponsePolicy",
    # Execution Containment (v0.9.0)
    "CancellationToken",
    "ChainMetadata",
    "ContextSnapshot",
    "ExecutionConfig",
    "ExecutionContext",
    "ExecutionGraph",
    "NodeEvent",
    "NodeRecord",
    "WrapOptions",
    # Partial buffer helpers (v0.11)
    "get_current_partial_buffer",
    "attach_partial_buffer",
    # BudgetAllocator (v1.6.0)
    "AllocationResult",
    "BudgetAllocator",
    "FairShareAllocator",
    "WeightedAllocator",
    "DynamicAllocator",
    # Execution boundary (v0.9.1)
    "AIContainer",
    # Decorator-based injection (v0.9.3)
    "veronica_guard",
    "GuardConfig",
    "VeronicaHalt",
    "is_guard_active",
    "get_active_container",
    # SDK Patch (v0.9.4)
    "patch_openai",
    "patch_anthropic",
    "unpatch_all",
    # Semantic Loop Guard (v0.9.6)
    "SemanticLoopGuard",
    # Auto Pricing (v0.10.0)
    "estimate_cost_usd",
    "resolve_model_pricing",
    "Pricing",
    "extract_usage_from_response",
    # Distributed Budget (v0.10.0)
    "BudgetBackend",
    "ReservableBudgetBackend",
    "LocalBudgetBackend",
    "RedisBudgetBackend",
    "get_default_backend",
    # Distributed Circuit Breaker (v1.1.0)
    "CircuitSnapshot",
    "DistributedCircuitBreaker",
    "get_default_circuit_breaker",
    # OpenTelemetry (v0.10.0)
    "enable_otel",
    "disable_otel",
    "is_otel_enabled",
    "enable_otel_with_provider",
    "OTelExecutionGraphObserver",
    # Degradation Ladder (v0.10.0)
    "DegradationLadder",
    "DegradationConfig",
    "Trimmer",
    "NoOpTrimmer",
    # PolicyDecision helpers (v0.10.0)
    "allow",
    "deny",
    "model_downgrade",
    "rate_limit_decision",
    # AG2 AgentCapability-compatible adapters (v0.11.0)
    "CircuitBreakerCapability",
    # MCP containment adapter (v1.6.0)
    "MCPContainmentAdapter",
    "MCPToolCost",
    "MCPToolResult",
    "MCPToolStats",
    # Async MCP containment (v1.7.0)
    "AsyncMCPContainmentAdapter",
    "wrap_mcp_server",
    # ASGI/WSGI Middleware (v0.11.0)
    "VeronicaASGIMiddleware",
    "VeronicaWSGIMiddleware",
    "get_current_execution_context",
    # Compliance Export (v1.4.0)
    "ComplianceExporter",
    # Audit Chain (v3.0.0)
    "AuditChain",
    "AuditEntry",
    # Quickstart API (v1.4.0)
    "init",
    "shutdown",
    "get_context",
    # Protocol definitions (v1.6.0)
    "FrameworkAdapterProtocol",
    "PlannerProtocol",
    "ExecutionGraphObserver",
    "ContainmentMetricsProtocol",
    # Adapter capabilities (v3.0.0)
    "AdapterCapabilities",
    # Metrics implementations (v1.6.0)
    "LoggingContainmentMetrics",
    # OTel Feedback Loop (v2.4)
    "AgentMetrics",
    "OTelMetricsIngester",
    "MetricRule",
    "MetricsDrivenPolicy",
    # Policy Simulation (v2.6.0)
    "ExecutionLog",
    "ExecutionLogEntry",
    "PolicySimulator",
    "SimulationEvent",
    "SimulationReport",
    # A2A Trust Boundary (v2.7)
    "TrustLevel",
    "AgentIdentity",
    "TrustPolicy",
    "TrustBasedPolicyRouter",
    "TrustEscalationTracker",
    "identity_from_a2a_card",
]
