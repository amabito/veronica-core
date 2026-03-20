"""VERONICA Core - Failsafe state machine for mission-critical applications."""

from __future__ import annotations

import importlib

__version__ = "3.8.1"

# ---------------------------------------------------------------------------
# Eager imports -- minimal core types needed at import time
# (kept small to avoid circular imports and slow startup)
# ---------------------------------------------------------------------------

# Core state machine (used everywhere, negligible import cost)
from veronica_core.state import (  # noqa: F401
    VeronicaState,
    StateTransition,
    VeronicaStateMachine,
)

# Execution containment core types (used in type annotations across the lib)
from veronica_core.containment import (  # noqa: F401
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)

# ---------------------------------------------------------------------------
# Lazy import registry
# Maps public name -> (module_path, attribute_name)
# ---------------------------------------------------------------------------

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Persistence backends
    "PersistenceBackend": ("veronica_core.backends", "PersistenceBackend"),
    "JSONBackend": ("veronica_core.backends", "JSONBackend"),
    "MemoryBackend": ("veronica_core.backends", "MemoryBackend"),
    # Exit handling
    "ExitTier": ("veronica_core.exit", "ExitTier"),
    "VeronicaExit": ("veronica_core.exit", "VeronicaExit"),
    # Guard interface
    "VeronicaGuard": ("veronica_core.guards", "VeronicaGuard"),
    "PermissiveGuard": ("veronica_core.guards", "PermissiveGuard"),
    # LLM client interface (optional)
    "LLMClient": ("veronica_core.clients", "LLMClient"),
    "NullClient": ("veronica_core.clients", "NullClient"),
    "DummyClient": ("veronica_core.clients", "DummyClient"),
    # Runtime Policy Control (v0.2)
    "RuntimePolicy": ("veronica_core.runtime_policy", "RuntimePolicy"),
    "PolicyContext": ("veronica_core.runtime_policy", "PolicyContext"),
    "PolicyDecision": ("veronica_core.runtime_policy", "PolicyDecision"),
    "PolicyPipeline": ("veronica_core.runtime_policy", "PolicyPipeline"),
    "allow": ("veronica_core.runtime_policy", "allow"),
    "deny": ("veronica_core.runtime_policy", "deny"),
    "model_downgrade": ("veronica_core.runtime_policy", "model_downgrade"),
    "rate_limit_decision": ("veronica_core.runtime_policy", "rate_limit_decision"),
    # LLM Safety modules
    "BudgetEnforcer": ("veronica_core.budget", "BudgetEnforcer"),
    "AgentStepGuard": ("veronica_core.agent_guard", "AgentStepGuard"),
    "PartialResultBuffer": ("veronica_core.partial", "PartialResultBuffer"),
    "RetryContainer": ("veronica_core.retry", "RetryContainer"),
    "CircuitBreaker": ("veronica_core.circuit_breaker", "CircuitBreaker"),
    "CircuitState": ("veronica_core.circuit_breaker", "CircuitState"),
    "FailurePredicate": ("veronica_core.circuit_breaker", "FailurePredicate"),
    "ignore_exception_types": (
        "veronica_core.circuit_breaker",
        "ignore_exception_types",
    ),
    "count_exception_types": ("veronica_core.circuit_breaker", "count_exception_types"),
    "ignore_status_codes": ("veronica_core.circuit_breaker", "ignore_status_codes"),
    # Integration API
    "VeronicaIntegration": ("veronica_core.integration", "VeronicaIntegration"),
    "get_veronica_integration": (
        "veronica_core.integration",
        "get_veronica_integration",
    ),
    # Execution Shield (v0.3)
    "ShieldConfig": ("veronica_core.shield", "ShieldConfig"),
    "PostDispatchHook": ("veronica_core.shield", "PostDispatchHook"),
    "SafeModeConfig": ("veronica_core.shield.config", "SafeModeConfig"),
    "BudgetConfig": ("veronica_core.shield.config", "BudgetConfig"),
    "CircuitBreakerConfig": ("veronica_core.shield.config", "CircuitBreakerConfig"),
    "EgressConfig": ("veronica_core.shield.config", "EgressConfig"),
    "SecretGuardConfig": ("veronica_core.shield.config", "SecretGuardConfig"),
    "BudgetWindowConfig": ("veronica_core.shield.config", "BudgetWindowConfig"),
    "TokenBudgetConfig": ("veronica_core.shield.config", "TokenBudgetConfig"),
    "InputCompressionConfig": ("veronica_core.shield.config", "InputCompressionConfig"),
    "AdaptiveBudgetConfig": ("veronica_core.shield.config", "AdaptiveBudgetConfig"),
    "TimeAwarePolicyConfig": ("veronica_core.shield.config", "TimeAwarePolicyConfig"),
    "BudgetWindowHook": ("veronica_core.shield.budget_window", "BudgetWindowHook"),
    "TokenBudgetHook": ("veronica_core.shield.token_budget", "TokenBudgetHook"),
    "InputCompressionHook": (
        "veronica_core.shield.input_compression",
        "InputCompressionHook",
    ),
    "AdaptiveBudgetHook": (
        "veronica_core.shield.adaptive_budget",
        "AdaptiveBudgetHook",
    ),
    "AdjustmentResult": ("veronica_core.shield.adaptive_budget", "AdjustmentResult"),
    "TimeAwarePolicy": ("veronica_core.shield.time_policy", "TimeAwarePolicy"),
    "TimeResult": ("veronica_core.shield.time_policy", "TimeResult"),
    "DegradationLadder": ("veronica_core.shield.degradation", "DegradationLadder"),
    "DegradationConfig": ("veronica_core.shield.degradation", "DegradationConfig"),
    "Trimmer": ("veronica_core.shield.degradation", "Trimmer"),
    "NoOpTrimmer": ("veronica_core.shield.degradation", "NoOpTrimmer"),
    # Runtime Policies (v0.4.3)
    "MinimalResponsePolicy": (
        "veronica_core.policies.minimal_response",
        "MinimalResponsePolicy",
    ),
    # Execution Containment extras (v0.9.0)
    "CancellationToken": ("veronica_core.containment", "CancellationToken"),
    "ChainMetadata": ("veronica_core.containment", "ChainMetadata"),
    "ContextSnapshot": ("veronica_core.containment", "ContextSnapshot"),
    "ExecutionGraph": ("veronica_core.containment", "ExecutionGraph"),
    "NodeEvent": ("veronica_core.containment", "NodeEvent"),
    "NodeRecord": ("veronica_core.containment", "NodeRecord"),
    "get_current_partial_buffer": (
        "veronica_core.containment",
        "get_current_partial_buffer",
    ),
    "attach_partial_buffer": ("veronica_core.containment", "attach_partial_buffer"),
    # BudgetAllocator (v1.6.0)
    "AllocationResult": ("veronica_core.containment", "AllocationResult"),
    "BudgetAllocator": ("veronica_core.containment", "BudgetAllocator"),
    "FairShareAllocator": ("veronica_core.containment", "FairShareAllocator"),
    "WeightedAllocator": ("veronica_core.containment", "WeightedAllocator"),
    "DynamicAllocator": ("veronica_core.containment", "DynamicAllocator"),
    # Execution boundary (v0.9.1)
    "AIContainer": ("veronica_core.container", "AIContainer"),
    # Decorator-based injection (v0.9.3)
    "veronica_guard": ("veronica_core.inject", "veronica_guard"),
    "GuardConfig": ("veronica_core.inject", "GuardConfig"),
    "VeronicaHalt": ("veronica_core.inject", "VeronicaHalt"),
    "is_guard_active": ("veronica_core.inject", "is_guard_active"),
    "get_active_container": ("veronica_core.inject", "get_active_container"),
    # SDK patch module (v0.9.4)
    "patch_openai": ("veronica_core.patch", "patch_openai"),
    "patch_anthropic": ("veronica_core.patch", "patch_anthropic"),
    "unpatch_all": ("veronica_core.patch", "unpatch_all"),
    # Semantic Loop Guard (v0.9.6)
    "SemanticLoopGuard": ("veronica_core.semantic", "SemanticLoopGuard"),
    # Auto Pricing (v0.10.0)
    "estimate_cost_usd": ("veronica_core.pricing", "estimate_cost_usd"),
    "resolve_model_pricing": ("veronica_core.pricing", "resolve_model_pricing"),
    "Pricing": ("veronica_core.pricing", "Pricing"),
    "extract_usage_from_response": (
        "veronica_core.pricing",
        "extract_usage_from_response",
    ),
    # Distributed Budget (v0.10.0)
    "BudgetBackend": ("veronica_core.distributed", "BudgetBackend"),
    "ReservableBudgetBackend": ("veronica_core.distributed", "ReservableBudgetBackend"),
    "LocalBudgetBackend": ("veronica_core.distributed", "LocalBudgetBackend"),
    "RedisBudgetBackend": ("veronica_core.distributed", "RedisBudgetBackend"),
    "get_default_backend": ("veronica_core.distributed", "get_default_backend"),
    "CircuitSnapshot": ("veronica_core.distributed", "CircuitSnapshot"),
    "DistributedCircuitBreaker": (
        "veronica_core.distributed",
        "DistributedCircuitBreaker",
    ),
    "get_default_circuit_breaker": (
        "veronica_core.distributed",
        "get_default_circuit_breaker",
    ),
    # OpenTelemetry (v0.10.0)
    "enable_otel": ("veronica_core.otel", "enable_otel"),
    "disable_otel": ("veronica_core.otel", "disable_otel"),
    "is_otel_enabled": ("veronica_core.otel", "is_otel_enabled"),
    "enable_otel_with_provider": ("veronica_core.otel", "enable_otel_with_provider"),
    "enable_otel_with_tracer": ("veronica_core.otel", "enable_otel_with_tracer"),
    "OTelExecutionGraphObserver": ("veronica_core.otel", "OTelExecutionGraphObserver"),
    # AG2 AgentCapability-compatible adapters (v0.11.0)
    "CircuitBreakerCapability": (
        "veronica_core.adapters.ag2_capability",
        "CircuitBreakerCapability",
    ),
    # MCP containment adapter (v1.6.0)
    "MCPContainmentAdapter": ("veronica_core.adapters.mcp", "MCPContainmentAdapter"),
    "MCPToolCost": ("veronica_core.adapters.mcp", "MCPToolCost"),
    "MCPToolResult": ("veronica_core.adapters.mcp", "MCPToolResult"),
    "MCPToolStats": ("veronica_core.adapters.mcp", "MCPToolStats"),
    # Async MCP containment (v1.7.0)
    "AsyncMCPContainmentAdapter": (
        "veronica_core.adapters.mcp_async",
        "AsyncMCPContainmentAdapter",
    ),
    "wrap_mcp_server": ("veronica_core.adapters.mcp_async", "wrap_mcp_server"),
    # ASGI/WSGI Middleware (v0.11.0)
    "VeronicaASGIMiddleware": ("veronica_core.middleware", "VeronicaASGIMiddleware"),
    "VeronicaWSGIMiddleware": ("veronica_core.middleware", "VeronicaWSGIMiddleware"),
    "get_current_execution_context": (
        "veronica_core.middleware",
        "get_current_execution_context",
    ),
    # Compliance Export (v1.4.0)
    "ComplianceExporter": ("veronica_core.compliance", "ComplianceExporter"),
    # Audit Chain (v3.0.0)
    "AuditChain": ("veronica_core.compliance.audit_chain", "AuditChain"),
    "AuditEntry": ("veronica_core.compliance.audit_chain", "AuditEntry"),
    # Quickstart API (v1.4.0)
    "init": ("veronica_core.quickstart", "init"),
    "shutdown": ("veronica_core.quickstart", "shutdown"),
    "get_context": ("veronica_core.quickstart", "get_context"),
    # Protocol definitions (v1.6.0)
    "ExtendedAdapterProtocol": ("veronica_core.protocols", "ExtendedAdapterProtocol"),
    "FrameworkAdapterProtocol": ("veronica_core.protocols", "FrameworkAdapterProtocol"),
    "PlannerProtocol": ("veronica_core.protocols", "PlannerProtocol"),
    "ExecutionGraphObserver": ("veronica_core.protocols", "ExecutionGraphObserver"),
    "ContainmentMetricsProtocol": (
        "veronica_core.protocols",
        "ContainmentMetricsProtocol",
    ),
    "AsyncBudgetBackendProtocol": (
        "veronica_core.protocols",
        "AsyncBudgetBackendProtocol",
    ),
    "ReconciliationCallback": ("veronica_core.protocols", "ReconciliationCallback"),
    # Adapter capabilities (v3.0.0)
    "AdapterCapabilities": (
        "veronica_core.adapter_capabilities",
        "AdapterCapabilities",
    ),
    # Metrics implementations (v1.6.0)
    "LoggingContainmentMetrics": ("veronica_core.metrics", "LoggingContainmentMetrics"),
    # OTel Feedback Loop (v2.4)
    "AgentMetrics": ("veronica_core.otel_feedback", "AgentMetrics"),
    "OTelMetricsIngester": ("veronica_core.otel_feedback", "OTelMetricsIngester"),
    "MetricRule": ("veronica_core.otel_feedback", "MetricRule"),
    "MetricsDrivenPolicy": ("veronica_core.otel_feedback", "MetricsDrivenPolicy"),
    # Policy Simulation (v2.6.0)
    "ExecutionLog": ("veronica_core.simulation", "ExecutionLog"),
    "ExecutionLogEntry": ("veronica_core.simulation", "ExecutionLogEntry"),
    "PolicySimulator": ("veronica_core.simulation", "PolicySimulator"),
    "SimulationEvent": ("veronica_core.simulation", "SimulationEvent"),
    "SimulationReport": ("veronica_core.simulation", "SimulationReport"),
    # A2A Trust Boundary (v2.7.0)
    "TrustLevel": ("veronica_core.a2a", "TrustLevel"),
    "AgentIdentity": ("veronica_core.a2a", "AgentIdentity"),
    "TrustPolicy": ("veronica_core.a2a", "TrustPolicy"),
    "TrustBasedPolicyRouter": ("veronica_core.a2a", "TrustBasedPolicyRouter"),
    "TrustEscalationTracker": ("veronica_core.a2a", "TrustEscalationTracker"),
    "identity_from_a2a_card": ("veronica_core.a2a", "identity_from_a2a_card"),
    # Security capability types (v3.0.0)
    "Capability": ("veronica_core.security.capabilities", "Capability"),
    # Policy-attested bundle foundation (v3.2)
    "PolicyBundle": ("veronica_core.policy.bundle", "PolicyBundle"),
    "PolicyMetadata": ("veronica_core.policy.bundle", "PolicyMetadata"),
    "PolicyRule": ("veronica_core.policy.bundle", "PolicyRule"),
    "PolicyVerifier": ("veronica_core.policy.verifier", "PolicyVerifier"),
    "VerificationResult": ("veronica_core.policy.verifier", "VerificationResult"),
    "FrozenPolicyView": ("veronica_core.policy.frozen_view", "FrozenPolicyView"),
    "PolicyViewHolder": ("veronica_core.policy.frozen_view", "PolicyViewHolder"),
    # Memory Governance ABI (v3.3.0)
    "MemoryAction": ("veronica_core.memory.types", "MemoryAction"),
    "MemoryProvenance": ("veronica_core.memory.types", "MemoryProvenance"),
    "MemoryOperation": ("veronica_core.memory.types", "MemoryOperation"),
    "MemoryPolicyContext": ("veronica_core.memory.types", "MemoryPolicyContext"),
    "GovernanceVerdict": ("veronica_core.memory.types", "GovernanceVerdict"),
    "MemoryGovernanceDecision": (
        "veronica_core.memory.types",
        "MemoryGovernanceDecision",
    ),
    "MemoryGovernanceHook": ("veronica_core.memory.hooks", "MemoryGovernanceHook"),
    "DefaultMemoryGovernanceHook": (
        "veronica_core.memory.hooks",
        "DefaultMemoryGovernanceHook",
    ),
    "DenyAllMemoryGovernanceHook": (
        "veronica_core.memory.hooks",
        "DenyAllMemoryGovernanceHook",
    ),
    "MemoryGovernor": ("veronica_core.memory.governor", "MemoryGovernor"),
    # Memory Boundary Hook (v3.4.0)
    "MemoryAccessRule": ("veronica_core.shield.memory_boundary", "MemoryAccessRule"),
    "MemoryBoundaryConfig": (
        "veronica_core.shield.memory_boundary",
        "MemoryBoundaryConfig",
    ),
    "MemoryBoundaryHook": (
        "veronica_core.shield.memory_boundary",
        "MemoryBoundaryHook",
    ),
    # Kernel Decision Envelope (v3.2+, exported v3.7.0)
    "DecisionEnvelope": ("veronica_core.kernel.decision", "DecisionEnvelope"),
    "ReasonCode": ("veronica_core.kernel.decision", "ReasonCode"),
    "make_envelope": ("veronica_core.kernel.decision", "make_envelope"),
    # Kernel HA ABI (v3.5.0, exported v3.7.0)
    "ReservationState": ("veronica_core.kernel.ha", "ReservationState"),
    "PolicyEpochStamp": ("veronica_core.kernel.ha", "PolicyEpochStamp"),
    "BreakerReflection": ("veronica_core.kernel.ha", "BreakerReflection"),
    "Reservation": ("veronica_core.kernel.ha", "Reservation"),
    "HeartbeatSnapshot": ("veronica_core.kernel.ha", "HeartbeatSnapshot"),
    # Kernel Startup Guard (v3.7.0)
    "verify_policy_or_halt": ("veronica_core.kernel.startup", "verify_policy_or_halt"),
    "load_and_verify": ("veronica_core.kernel.startup", "load_and_verify"),
    # Kernel Audit Bridge (v3.7.0)
    "emit_governance_event": (
        "veronica_core.kernel.audit_bridge",
        "emit_governance_event",
    ),
    "should_emit": ("veronica_core.kernel.audit_bridge", "should_emit"),
    # Memory View / Execution Mode / DEGRADE (v3.6.0)
    "MemoryView": ("veronica_core.memory.types", "MemoryView"),
    "ExecutionMode": ("veronica_core.memory.types", "ExecutionMode"),
    "DegradeDirective": ("veronica_core.memory.types", "DegradeDirective"),
    "CompactnessConstraints": ("veronica_core.memory.types", "CompactnessConstraints"),
    "MessageContext": ("veronica_core.memory.types", "MessageContext"),
    "BridgePolicy": ("veronica_core.memory.types", "BridgePolicy"),
    "ThreatContext": ("veronica_core.memory.types", "ThreatContext"),
    # Memory Evaluators (v3.6.0)
    "CompactnessEvaluator": (
        "veronica_core.memory.compactness",
        "CompactnessEvaluator",
    ),
    "ViewPolicyEvaluator": ("veronica_core.memory.view_policy", "ViewPolicyEvaluator"),
    # Message Governance (v3.6.0)
    "MessageGovernanceHook": (
        "veronica_core.memory.message_governance",
        "MessageGovernanceHook",
    ),
    "DefaultMessageGovernanceHook": (
        "veronica_core.memory.message_governance",
        "DefaultMessageGovernanceHook",
    ),
    "DenyOversizedMessageHook": (
        "veronica_core.memory.message_governance",
        "DenyOversizedMessageHook",
    ),
    "MessageBridgeHook": (
        "veronica_core.memory.message_governance",
        "MessageBridgeHook",
    ),
    # Scoped execution mode context manager (v3.7.0)
    "scoped_execution_mode": ("veronica_core.memory.types", "scoped_execution_mode"),
    # Memory Lifecycle (v3.7.0)
    "ProvenanceLifecycle": ("veronica_core.memory.lifecycle", "ProvenanceLifecycle"),
    "TransitionResult": ("veronica_core.memory.lifecycle", "TransitionResult"),
    # Memory Policy Rules (v3.7.0)
    "MemoryRuleCompiler": ("veronica_core.policy.memory_rules", "MemoryRuleCompiler"),
    "CompiledMemoryRule": ("veronica_core.policy.memory_rules", "CompiledMemoryRule"),
    "MemoryRuleEvaluator": ("veronica_core.policy.memory_rules", "MemoryRuleEvaluator"),
    # Diagnostics (v3.7.0)
    "MemoryGovernanceReadiness": (
        "veronica_core.diagnostics.readiness",
        "MemoryGovernanceReadiness",
    ),
    "ReadinessSnapshot": ("veronica_core.diagnostics.readiness", "ReadinessSnapshot"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        # Cache in module globals so subsequent accesses bypass __getattr__
        globals()[name] = val
        return val
    raise AttributeError(f"module 'veronica_core' has no attribute {name!r}")


# ---------------------------------------------------------------------------
# __all__ -- derived from eager imports + lazy registry (single source of truth)
# ---------------------------------------------------------------------------

_EAGER_EXPORTS = [
    "__version__",
    "VeronicaState",
    "StateTransition",
    "VeronicaStateMachine",
    "ExecutionConfig",
    "ExecutionContext",
    "WrapOptions",
]

__all__ = _EAGER_EXPORTS + sorted(_LAZY_IMPORTS)


def __dir__() -> list[str]:
    return list(__all__)
