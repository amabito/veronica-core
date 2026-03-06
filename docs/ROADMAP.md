# veronica-core Technical Roadmap

**Version**: 3.0.3 -> 4.x
**Date**: 2026-03-06
**Author**: Codebase analysis (full src/ inspection)
**Scope**: Systems architecture roadmap, not feature wishlist

---

## 1. Architecture Map

```
                        User Application
                              |
                    +---------+---------+
                    |   Quickstart API  |   init() / shutdown() / get_context()
                    +---------+---------+
                              |
               +--------------+--------------+
               |      VeronicaIntegration    |   Unified facade
               +--------------+--------------+
                              |
          +-------------------+-------------------+
          |                   |                   |
  +-------+-------+  +-------+-------+  +-------+--------+
  | Framework     |  | Middleware    |  | Decorator      |
  | Adapters (8)  |  | ASGI / WSGI   |  | @veronica_guard|
  +-------+-------+  +-------+-------+  +-------+--------+
          |                   |                   |
          +-------------------+-------------------+
                              |
               +--------------+--------------+
               |      ExecutionContext       |   Chain-level containment
               |  cost / steps / retries     |   CancellationToken
               |  timeout / circuit state    |   PartialResultBuffer
               +--------------+--------------+
                              |
          +-------------------+-------------------+
          |                   |                   |
  +-------+-------+  +-------+-------+  +-------+--------+
  | ShieldPipeline|  | ExecutionGraph|  | BudgetAllocator|
  | Hook chain    |  | DAG tracking  |  | Fair/Weighted  |
  +-------+-------+  +-------+-------+  +-------+--------+
          |
  +-------+-------+-------+-------+-------+
  |       |       |       |       |       |
Budget  Token  Circuit  Retry  Egress  Tool
 Hook   Hook   Breaker  Hook    Hook   Hook
  |       |       |
  +-------+-------+
          |
  +-------+-------+
  | Distributed   |
  | Redis + Lua   |
  | Backend       |
  +-------+-------+

Cross-cutting:
  PolicyEngine (security)     -> shell/file/net/git/browser rules
  A2A TrustBoundary           -> TrustLevel -> PolicyRouter -> ShieldPipeline
  AuditChain (SHA-256)        -> append-only hash chain
  OTel (spans + feedback)     -> MetricsDrivenPolicy
  Simulation                  -> replay logs against policies
  Compliance                  -> export + dashboard
  Adaptive                    -> BurnRate + Anomaly + ThresholdPolicy
  Tenant                      -> hierarchy + budget pools
```

### Single LLM call flow

1. Framework adapter intercepts SDK call (e.g. `patch_openai()`)
2. Adapter calls `ExecutionContext.wrap_llm_call()`
3. ExecutionContext checks: budget, steps, retries, timeout, circuit breaker
4. If any limit hit -> return `Decision.HALT` or `Decision.DEGRADE`
5. If ALLOW -> ShieldPipeline runs pre-dispatch hooks (BudgetWindow, TokenBudget, Egress, etc.)
6. If all hooks ALLOW -> call proceeds to LLM provider
7. Post-dispatch hooks record cost, tokens, latency
8. ExecutionGraph updates node status (running -> success/fail)
9. AuditChain appends entry
10. OTel span emitted if enabled

---

## 2. Codebase Evaluation

### What the project does well

**Deterministic containment**: Every decision path returns `Decision` enum (ALLOW/DEGRADE/HALT). No advisory-only modes. HALT means the call does not execute.

**Protocol-driven design**: 21+ `@runtime_checkable` protocols. Any component is replaceable. `CircuitBreaker`, `BudgetEnforcer`, `AgentStepGuard` all implement `RuntimePolicy`. Framework adapters implement `FrameworkAdapterProtocol`. Budget backends implement `BudgetBackend`.

**Zero-dependency core**: `pyproject.toml` has zero required dependencies. Redis, OTel, Vault, MCP are all extras. This is rare for a library of this scope and correctly done.

**Distributed atomicity**: `DistributedCircuitBreaker` and `RedisBudgetBackend` use Lua scripts for atomic Redis operations. No TOCTOU in the critical path. `reserve/commit/rollback` two-phase budget.

**Test discipline**: 3,907 tests. 92% coverage. 20-scenario red-team regression suite. 3 rounds of independent security audits (68 + 13 + 27 findings, all fixed). Adversarial test categories: thundering herd, TOCTOU, resource exhaustion, state corruption.

**Audit trail**: SHA-256 hash chain (`AuditChain`) with append-only semantics. Thread-safe. JSON export/import. Separate from logging -- structural integrity.

**Explicit deferred items**: `V2_DEFERRED.md` documents 20 latent edge cases, 5 design tradeoffs, and 7 architectural items with severity ratings. Transparent about what is NOT fixed and why.

---

## 3. Weakness Analysis

### W-1: API surface bloat (MEDIUM)

`__init__.py` exports 140 symbols. For a kernel library, this is too many. Users face a flat namespace with no hierarchy guidance. `BudgetEnforcer`, `BudgetWindowHook`, `BudgetConfig`, `BudgetAllocator`, `AdaptiveBudgetHook`, `AdaptiveBudgetConfig` all appear at the top level.

**Impact**: New users cannot distinguish core from peripheral. Import autocompletion is noisy.

**Root cause**: D-3 in V2_DEFERRED.md notes this. Eager-loading 140 symbols also increases import time.

### W-2: execution_context.py is still 1,536 lines (MEDIUM)

v3.0.0 "God Class Split" refactored but `execution_context.py` remains the largest file at 1,536 lines. It handles: budget tracking, step counting, retry tracking, timeout management, circuit breaker binding, child context creation, partial result buffering, snapshot serialization, and graph integration.

**Impact**: High cognitive load for contributors. Merge conflicts on concurrent changes.

**Root cause**: D-4 in V2_DEFERRED.md. The split was partial -- types and graph were extracted, but the core context manager was not decomposed.

### W-3: `adapter/` vs `adapters/` directory split (LOW)

Two separate directories exist: `adapter/exec.py` (278 lines) and `adapters/*.py` (8 framework adapters). This is confusing.

**Root cause**: D-1 in V2_DEFERRED.md.

### W-4: No conftest.py / shared fixtures (LOW)

157 test files with no shared `conftest.py`. Fixtures are duplicated per-file. `_make_ctx()`, `_make_pipeline()` patterns are repeated.

**Impact**: Test maintenance burden. Fixture drift between test files.

### W-5: `time.time()` instead of `time.monotonic()` (LOW)

Timeouts in `execution_context.py` and `circuit_breaker.py` use `time.time()`. Clock adjustments (NTP, DST) can cause premature or delayed timeout.

**Root cause**: L-17 in V2_DEFERRED.md.

### W-6: ExecutionGraph unbounded growth (LOW)

`ExecutionGraph._nodes` dict grows without limit (L-18). For short-lived chains this is fine. For long-running daemons processing thousands of requests through a single context, memory grows linearly.

### W-7: persist module test gap (LOW)

`persist.py` has only 5 inline tests. No dedicated test file (D-9).

### W-8: nogil Python readiness (FUTURE)

Several documented tradeoffs (T-5, L-6) assume CPython GIL. PEP 703 (nogil, Python 3.13+) will break these assumptions. `CircuitBreaker` failure predicate evaluation outside lock, `ComplianceExporter._attached` flag, and `BudgetEnforcer` limit check are all GIL-dependent.

---

## 4. System Role Definition

veronica-core is a **runtime containment engine**.

Not a guardrail library -- guardrails (Guardrails AI, NeMo) validate LLM output content. VERONICA does not inspect prompt/completion content. It enforces execution boundaries: cost, steps, retries, timeouts, circuit state.

Not an AI execution OS -- it does not schedule, route, or orchestrate agents. It constrains them.

Not an observability tool -- OTel integration exists but as a feedback input, not the primary function.

The correct analogy is a **resource governor** (like Linux cgroups for containers, or database query governors). It sits between the agent and the LLM provider, enforcing hard limits on resource consumption. The agent decides what to do; VERONICA decides whether it is allowed to do it.

This role is architecturally correct. The codebase maintains this boundary consistently -- no policy inspects LLM output content, no adapter modifies prompts, no component makes routing decisions.

---

## 5. Future Architecture

```
                    Agent Framework (AG2, LangChain, CrewAI)
                              |
                    +---------+---------+
                    |   VERONICA        |   Runtime containment
                    |   (this project)  |   Budget, circuit, trust
                    +---------+---------+
                              |
               +--------------+--------------+
               |                             |
      +--------+---------+          +--------+--------+
      | Model Layer      |          | Tool Execution  |
      | (LLM providers)  |          | (MCP servers)   |
      +--------+---------+          +--------+--------+
               |                             |
      +--------+---------+          +--------+--------+
      | Memory System    |          | External APIs   |
      | (TriMemory etc.) |          | (databases, web)|
      +------------------+          +-----------------+
```

VERONICA's position is between the agent framework and everything below it. Every outbound call (LLM, tool, memory read/write) passes through ExecutionContext.

**Memory governance integration**: If a memory system (e.g. TriMemory) stores per-agent context, VERONICA should enforce:
- Read budget (prevent memory-bombing attacks where an agent forces expensive retrieval)
- Write rate limits (prevent memory pollution)
- Memory isolation between trust levels (A2A agents cannot read PRIVILEGED memory)

This requires extending `ToolCallContext` to distinguish memory operations from general tool calls, and adding `MemoryBoundaryHook` to the ShieldPipeline.

**Federation**: Multiple VERONICA instances coordinating across organizations. This is Phase G in the existing roadmap and remains the correct long-term target.

---

## 6. Development Roadmap

### Phase 1: Kernel Stabilization (v3.1)

**Goal**: Resolve all documented deferred items that affect API stability and developer experience.

**Features**:
- **Lazy imports for `__init__.py`** (D-3): Replace 140 eager imports with `__getattr__`-based lazy loading. Group exports into sub-namespaces: `veronica_core.shield`, `veronica_core.distributed`, `veronica_core.adapters`, `veronica_core.a2a`. Keep top-level re-exports for backward compat but emit DeprecationWarning for peripheral symbols.
- **Unify `adapter/` and `adapters/`** (D-1): Move `adapter/exec.py` into `adapters/`. One directory, one concern.
- **`time.monotonic()` migration** (L-17): Replace `time.time()` in `execution_context.py`, `circuit_breaker.py`, `distributed_circuit_breaker.py` timeout paths.
- **conftest.py with shared fixtures**: Extract common patterns (`_make_ctx`, `_make_pipeline`, `_make_agent`) into `tests/conftest.py`.
- **persist module test backfill** (D-9): Dedicated `test_persist.py`.
- **BudgetEnforcer `limit_usd=0.0` edge case** (L-11): Return 100% utilization instead of `inf`.

**Risks**: Lazy imports can break runtime `isinstance()` checks if not careful. Test with all adapters.

**Benefit**: Clean API surface. Faster import time. Resolved tech debt.

### Phase 2: ExecutionContext Decomposition (v3.2)

**Goal**: Break the 1,536-line god class into focused, testable sub-modules.

**Features**:
- **Extract BudgetTracker**: Cost accumulation, budget checking, reserve/commit lifecycle. ~300 lines.
- **Extract StepTracker**: Step counting, max_steps enforcement. ~100 lines.
- **Extract RetryTracker**: Retry counting, retry policy. ~100 lines.
- **Extract TimeoutManager**: Timeout enforcement, `time.monotonic()` checks. ~150 lines.
- **ExecutionContext becomes orchestrator**: Delegates to trackers. ~400 lines.

**Architecture change**:
```python
class ExecutionContext:
    def __init__(self, config: ExecutionConfig, ...):
        self._budget = BudgetTracker(config.max_cost_usd, backend)
        self._steps = StepTracker(config.max_steps)
        self._retries = RetryTracker(config.max_retries_total)
        self._timeout = TimeoutManager(config.timeout_ms)
        self._graph = ExecutionGraph()

    def wrap_llm_call(self, ...):
        self._timeout.check()
        self._steps.increment()
        self._budget.check(cost_estimate)
        self._retries.check()
        # ... proceed
```

**Risks**: Behavioral regression in the most critical class. Require 100% existing test pass before merge.

**Benefit**: Each tracker independently testable. Contributor onboarding easier. Merge conflict surface reduced.

### Phase 3: nogil Python Readiness (v3.3)

**Goal**: Prepare for PEP 703 (free-threaded Python, expected 3.14/3.15).

**Features**:
- **Audit all GIL-dependent patterns**: T-5 (CircuitBreaker predicate outside lock), L-6 (ComplianceExporter._attached), budget check atomicity.
- **Add explicit locks where GIL was assumed**: `CircuitBreaker.record_failure()` predicate evaluation, `BudgetEnforcer` limit check-and-update.
- **Test under `PYTHON_GIL=0`**: CI matrix entry for free-threaded Python build.
- **ExecutionGraph node pruning** (L-18): Add `max_nodes` config with LRU eviction for long-running contexts.

**Risks**: Performance regression from added locks. Benchmark before/after.

**Benefit**: Future-proof for Python 3.14+. Eliminates a class of latent concurrency bugs.

### Phase 4: Federation -- Multi-Process Policy Coordination (v4.0)

**Goal**: Multiple VERONICA instances share budget state and policy decisions across processes/organizations.

This is Phase G from `EVOLUTION_ROADMAP.md`. The design is sound:

**Features**:
- **FederationNode**: Process-level identity with cryptographic attestation.
- **BudgetGrant**: Time-limited, policy-constrained budget delegation between nodes.
- **FederationGateway**: HTTP/gRPC transport (extras dependency: `pip install veronica-core[federation]`).
- **mTLS mutual authentication**: Nodes verify each other's identity.
- **Grant lifecycle**: request -> approve -> use -> report -> expire/revoke.
- **Policy propagation**: Grantor's policy constraints travel with the grant.

**Architecture change**:
```
Node A (org-alpha)                    Node B (org-beta)
  VERONICA instance                     VERONICA instance
       |                                     |
       +---- FederationGateway (mTLS) ------+
       |                                     |
  BudgetGrant {                         Uses grant within
    grantor: A,                         policy constraints
    amount: $50,
    expires: +1h,
    policy: ShieldPipeline
  }
```

**Prerequisites**: Phase E (A2A trust boundary) is complete. PolicySignerV2 exists. All needed primitives are in place.

**Risks**: Network partition handling. Grant revocation propagation delay. Double-spend across partitioned nodes.

**Mitigation**: Pessimistic grant model -- grantee cannot exceed granted amount even if grantor is unreachable. Grants expire on timeout.

**Benefit**: Cross-organization agent collaboration with budget accountability. Enterprise deployment pattern.

### Phase 5: Adapter Ecosystem Hardening (v4.1)

**Goal**: Stabilize adapter API and reduce per-adapter maintenance burden.

**Features**:
- **Adapter test harness**: Generic test suite that any adapter must pass. Parameterized by adapter class. Covers: cost extraction, HALT propagation, DEGRADE behavior, async support, error handling.
- **Adapter versioning**: Each adapter declares which framework version range it supports. CI tests against min/max supported versions.
- **Adapter generator**: Scaffold tool for new adapters (`veronica new-adapter --framework=X`).
- **Remove `_shared.py` duplication**: Extract token/cost extraction into composable mixins.

**Risks**: Breaking existing adapter implementations. Require migration period with deprecation warnings.

**Benefit**: New framework adapters in <100 lines. Community contributions easier.

### Phase 6: Memory Governance Integration (v4.2)

**Goal**: Extend containment to memory system operations.

**Features**:
- **MemoryBoundaryHook**: New hook type for ShieldPipeline. Controls read/write to agent memory stores.
- **Memory operation classification**: Extend `ToolCallContext` with `kind: Literal["llm", "tool", "memory_read", "memory_write"]`.
- **Trust-based memory isolation**: A2A agents at UNTRUSTED level cannot read TRUSTED memory. Enforced via TrustBasedPolicyRouter.
- **Memory cost tracking**: Memory retrieval (embedding search, vector DB queries) counted toward budget.

**Prerequisites**: Phase 4 (federation) for cross-org memory boundaries.

**Risks**: Over-engineering if no memory system integration materializes. Build only when concrete integration target exists (TriMemory, Mem0, etc.).

**Benefit**: Complete containment -- no execution path (LLM, tool, memory) bypasses governance.

---

## 7. Priority List

### Critical (must fix first)

| Item | Phase | Reason |
|------|-------|--------|
| Lazy imports / API surface cleanup | 1 | 140 symbols at top level hurts adoption. First impression matters for OSS. |
| `adapter/` vs `adapters/` unification | 1 | Confusing directory structure for new contributors. |
| `time.monotonic()` migration | 1 | `time.time()` is objectively wrong for timeouts. NTP adjustment can break containment guarantees. |

### Important (high value, moderate effort)

| Item | Phase | Reason |
|------|-------|--------|
| ExecutionContext decomposition | 2 | 1,536-line god class is the biggest maintainability risk. |
| conftest.py shared fixtures | 1 | 157 test files with duplicated setup. Test maintenance scales poorly. |
| persist module test backfill | 1 | Only 5 tests for a persistence module. Coverage gap in a safety library. |
| nogil audit | 3 | Python 3.14 free-threading will ship within 12 months. Prepare now. |
| Adapter test harness | 5 | 8 adapters with no shared test contract. Each tested differently. |

### Nice to have (low urgency, build when needed)

| Item | Phase | Reason |
|------|-------|--------|
| Federation | 4 | No concrete demand yet. Design is ready. Build when first enterprise customer needs cross-org. |
| Memory governance | 6 | Depends on a memory system existing to integrate with. |
| Adapter generator scaffold | 5 | Quality-of-life for community contributors. |
| ExecutionGraph pruning | 3 | Only matters for long-running daemon use cases. |
| BudgetEnforcer `limit_usd=0.0` fix | 1 | Cosmetic edge case. JSON serialization workaround exists. |

---

## 8. Summary

veronica-core is a mature runtime containment engine with strong architectural foundations. The codebase delivers on every claim in the README. Security audits are thorough and transparent. Test coverage exceeds industry standards.

The primary risks are not in functionality but in developer experience and long-term maintainability: API surface bloat, god class in the critical path, GIL-dependent thread safety, and adapter ecosystem fragmentation.

The roadmap prioritizes kernel stabilization (v3.1-3.3) before feature expansion (v4.0+). Federation is the correct long-term direction but should be demand-driven, not speculative.

Estimated timeline (Claude Code assisted):
- Phase 1 (v3.1): 2-3 days
- Phase 2 (v3.2): 3-4 days
- Phase 3 (v3.3): 2-3 days
- Phase 4 (v4.0): 5-7 days
- Phase 5 (v4.1): 3-4 days
- Phase 6 (v4.2): 3-4 days

Total: ~20-25 days, gated on actual demand for Phase 4+.
