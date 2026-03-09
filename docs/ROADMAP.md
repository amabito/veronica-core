# veronica-core Technical Roadmap

**Version**: 3.0.3 -> 4.x
**Date**: 2026-03-06
**Scope**: Systems architecture roadmap

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

## 2. Current State

veronica-core v3.4.2. Stable kernel release.

- 21+ `@runtime_checkable` protocols
- Zero required dependencies
- 4844 tests, 94% coverage
- 4 rounds of independent security audit (130+ findings, all fixed)
- Zero breaking changes from v2.1.0 through v3.4.2
- `V2_DEFERRED.md` documents 20 latent edge cases, 5 design tradeoffs, and 7 architectural items

v3.x is the containment kernel. It is feature-complete for single-process, single-organization use.

v4.0 (Federation) is scoped but not scheduled. It will be built when the first concrete cross-organization use case appears.

---

## 3. Weakness Analysis

### W-1: API surface bloat (MEDIUM)

`__init__.py` exports 140 symbols. Users face a flat namespace with no hierarchy guidance. `BudgetEnforcer`, `BudgetWindowHook`, `BudgetConfig`, `BudgetAllocator`, `AdaptiveBudgetHook`, `AdaptiveBudgetConfig` all appear at the top level.

**Impact**: Import autocompletion is noisy. New users cannot distinguish core from peripheral.

**Root cause**: D-3 in V2_DEFERRED.md. Eager-loading 140 symbols also increases import time.

### W-2: execution_context.py is still 1,536 lines (MEDIUM)

v3.0.0 "God Class Split" refactored but `execution_context.py` remains the largest file at 1,536 lines. It handles: budget tracking, step counting, retry tracking, timeout management, circuit breaker binding, child context creation, partial result buffering, snapshot serialization, and graph integration.

**Impact**: High cognitive load. Merge conflicts on concurrent changes.

**Root cause**: D-4 in V2_DEFERRED.md. The split was partial -- types and graph were extracted, but the core context manager was not decomposed.

### W-3: `adapter/` vs `adapters/` directory split (LOW)

Two separate directories: `adapter/exec.py` (278 lines) and `adapters/*.py` (8 framework adapters). Confusing.

**Root cause**: D-1 in V2_DEFERRED.md.

### W-4: No conftest.py / shared fixtures (LOW)

157 test files with no shared `conftest.py`. Fixtures are duplicated per-file. `_make_ctx()`, `_make_pipeline()` patterns are repeated.

### W-5: `time.time()` instead of `time.monotonic()` (LOW)

Timeouts in `execution_context.py` and `circuit_breaker.py` use `time.time()`. Clock adjustments (NTP, DST) can cause premature or delayed timeout.

**Root cause**: L-17 in V2_DEFERRED.md.

### W-6: ExecutionGraph unbounded growth (LOW)

`ExecutionGraph._nodes` dict grows without limit (L-18). For long-running daemons, memory grows linearly.

### W-7: persist module test gap (LOW)

`persist.py` has only 5 inline tests. No dedicated test file (D-9).

### W-8: nogil Python readiness (FUTURE)

Several documented tradeoffs (T-5, L-6) assume CPython GIL. PEP 703 (nogil, Python 3.13+) will break these assumptions. `CircuitBreaker` failure predicate evaluation outside lock, `ComplianceExporter._attached` flag, and `BudgetEnforcer` limit check are all GIL-dependent.

---

## 4. System Role

veronica-core is a **runtime containment engine**.

Not a guardrail library -- guardrails (Guardrails AI, NeMo) validate LLM output content. VERONICA does not inspect prompt/completion content. It enforces execution boundaries: cost, steps, retries, timeouts, circuit state.

Not an AI execution OS -- it does not schedule, route, or orchestrate agents. It constrains them.

The analogy: a resource governor (like Linux cgroups for containers, or database query governors). Sits between the agent and the LLM provider, enforcing hard limits on resource consumption.

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

VERONICA sits between the agent framework and everything below it. Every outbound call (LLM, tool, memory read/write) passes through ExecutionContext.

**Memory governance**: If a memory system stores per-agent context, VERONICA should enforce read budget, write rate limits, and memory isolation between trust levels. Requires extending `ToolCallContext` to distinguish memory operations from general tool calls, and adding `MemoryBoundaryHook` to the ShieldPipeline.

**Federation**: Multiple VERONICA instances coordinating across organizations. Phase G in `EVOLUTION_ROADMAP.md`.

---

## 6. Development Roadmap

### Phase 1: Kernel Stabilization (v3.1)

**Goal**: Resolve deferred items that affect API stability and developer experience.

- **Lazy imports for `__init__.py`** (D-3): Replace 140 eager imports with `__getattr__`-based lazy loading. Group exports into sub-namespaces: `veronica_core.shield`, `veronica_core.distributed`, `veronica_core.adapters`, `veronica_core.a2a`. Keep top-level re-exports for backward compat but emit DeprecationWarning for peripheral symbols.
- **Unify `adapter/` and `adapters/`** (D-1): Move `adapter/exec.py` into `adapters/`. One directory, one concern.
- **`time.monotonic()` migration** (L-17): Replace `time.time()` in `execution_context.py`, `circuit_breaker.py`, `distributed_circuit_breaker.py` timeout paths.
- **conftest.py with shared fixtures**: Extract common patterns (`_make_ctx`, `_make_pipeline`, `_make_agent`) into `tests/conftest.py`.
- **persist module test backfill** (D-9): Dedicated `test_persist.py`.
- **BudgetEnforcer `limit_usd=0.0` edge case** (L-11): Return 100% utilization instead of `inf`.

**Risks**: Lazy imports can break runtime `isinstance()` checks. Test with all adapters.

### Phase 2: ExecutionContext Decomposition (v3.2)

**Goal**: Break the 1,536-line god class into focused sub-modules.

- **Extract BudgetTracker**: Cost accumulation, budget checking, reserve/commit lifecycle. ~300 lines.
- **Extract StepTracker**: Step counting, max_steps enforcement. ~100 lines.
- **Extract RetryTracker**: Retry counting, retry policy. ~100 lines.
- **Extract TimeoutManager**: Timeout enforcement, `time.monotonic()` checks. ~150 lines.
- **ExecutionContext becomes orchestrator**: Delegates to trackers. ~400 lines.

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

### Phase 3: nogil Python Readiness (v3.3)

**Goal**: Prepare for PEP 703 (free-threaded Python, expected 3.14/3.15).

- **Audit all GIL-dependent patterns**: T-5 (CircuitBreaker predicate outside lock), L-6 (ComplianceExporter._attached), budget check atomicity.
- **Add explicit locks where GIL was assumed**: `CircuitBreaker.record_failure()` predicate evaluation, `BudgetEnforcer` limit check-and-update.
- **Test under `PYTHON_GIL=0`**: CI matrix entry for free-threaded Python build.
- **ExecutionGraph node pruning** (L-18): Add `max_nodes` config with LRU eviction for long-running contexts.

**Risks**: Performance regression from added locks. Benchmark before/after.

### Phase 4: Federation (v4.0) -- NOT SCHEDULED

**Goal**: Multiple VERONICA instances share budget state and policy decisions across processes/organizations.

From Phase G in `EVOLUTION_ROADMAP.md`:

- **FederationNode**: Process-level identity with cryptographic attestation.
- **BudgetGrant**: Time-limited, policy-constrained budget delegation between nodes.
- **FederationGateway**: HTTP/gRPC transport (extras: `pip install veronica-core[federation]`).
- **mTLS mutual authentication**: Nodes verify each other's identity.
- **Grant lifecycle**: request -> approve -> use -> report -> expire/revoke.
- **Policy propagation**: Grantor's policy constraints travel with the grant.

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

**Prerequisites**: Phase E (A2A trust boundary) complete. PolicySignerV2 exists.

**Risks**: Network partition handling. Grant revocation propagation delay. Double-spend across partitioned nodes. Mitigation: pessimistic grant model -- grantee cannot exceed granted amount even if grantor is unreachable. Grants expire on timeout.

### Phase 5: Adapter Ecosystem Hardening (v4.1) -- NOT SCHEDULED

**Goal**: Stabilize adapter API and reduce per-adapter maintenance burden.

- **Adapter test harness**: Generic test suite that any adapter must pass. Parameterized by adapter class. Covers: cost extraction, HALT propagation, DEGRADE behavior, async support, error handling.
- **Adapter versioning**: Each adapter declares which framework version range it supports. CI tests against min/max supported versions.
- **Adapter generator**: Scaffold tool for new adapters (`veronica new-adapter --framework=X`).
- **Remove `_shared.py` duplication**: Extract token/cost extraction into composable mixins.

**Risks**: Breaking existing adapter implementations. Migration period with deprecation warnings.

### Phase 6: Memory Governance Integration (v4.2) -- NOT SCHEDULED

**Goal**: Extend containment to memory system operations.

- **MemoryBoundaryHook**: New hook type for ShieldPipeline. Controls read/write to agent memory stores.
- **Memory operation classification**: Extend `ToolCallContext` with `kind: Literal["llm", "tool", "memory_read", "memory_write"]`.
- **Trust-based memory isolation**: A2A agents at UNTRUSTED level cannot read TRUSTED memory. Enforced via TrustBasedPolicyRouter.
- **Memory cost tracking**: Memory retrieval (embedding search, vector DB queries) counted toward budget.

**Prerequisites**: Phase 4 (federation) for cross-org memory boundaries.

**Risks**: Over-engineering if no memory system integration materializes. Build only when concrete integration target exists.

---

## 7. Priority

### Critical

| Item | Phase | Reason |
|------|-------|--------|
| Lazy imports / API surface cleanup | 1 | 140 symbols at top level hurts adoption |
| `adapter/` vs `adapters/` unification | 1 | Confusing for new contributors |
| `time.monotonic()` migration | 1 | `time.time()` is wrong for timeouts -- NTP adjustment breaks containment |

### Important

| Item | Phase | Reason |
|------|-------|--------|
| ExecutionContext decomposition | 2 | 1,536-line god class, biggest maintainability risk |
| conftest.py shared fixtures | 1 | 157 test files with duplicated setup |
| persist module test backfill | 1 | 5 tests for a persistence module in a safety library |
| nogil audit | 3 | Python 3.14 free-threading ships within 12 months |
| Adapter test harness | 5 | 8 adapters with no shared test contract |

### Build when needed

| Item | Phase | Reason |
|------|-------|--------|
| Federation | 4 | No concrete demand yet. Build when first cross-org use case appears |
| Memory governance | 6 | Depends on a memory system to integrate with |
| Adapter generator scaffold | 5 | Quality-of-life for contributors |
| ExecutionGraph pruning | 3 | Only matters for long-running daemon use cases |
| BudgetEnforcer `limit_usd=0.0` fix | 1 | Cosmetic edge case |
