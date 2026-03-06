# veronica-core Execution Plan

GitHub milestones, issues, implementation order, and architecture guardrails.

Derived from `docs/ROADMAP.md`. Each section maps directly to GitHub objects.

---

## 1. Milestones

### v3.1 -- Kernel Stabilization

**Goal**: Resolve tech debt that blocks API stability and contributor onboarding.

**Scope**:
- Lazy imports (`__getattr__`) for `__init__.py` (140 symbols -> tiered namespaces)
- Merge `adapter/` into `adapters/`
- `time.time()` -> `time.monotonic()` in all timeout paths
- Shared `conftest.py` with common test fixtures
- `persist.py` test backfill
- `BudgetEnforcer` `limit_usd=0.0` edge case

**Non-goals**:
- No new features
- No new adapters
- No behavioral changes to `ExecutionContext`

**Success criteria**:
- `from veronica_core import ExecutionContext` still works (backward compat)
- `from veronica_core.shield import BudgetWindowHook` works (new namespace)
- Old peripheral imports emit `DeprecationWarning`
- `time.monotonic()` used in all 3 timeout paths (execution_context, circuit_breaker, distributed_circuit_breaker)
- Single `adapters/` directory, zero files in `adapter/`
- `tests/conftest.py` exists with 5+ shared fixtures
- `tests/test_persist.py` exists with 15+ tests
- All 4045+ existing tests pass
- Import time reduced (measure before/after with `python -X importtime`)

---

### v3.2 -- ExecutionContext Decomposition

**Goal**: Break 1,536-line god class into focused sub-modules.

**Scope**:
- Extract `BudgetTracker` (~300 lines)
- Extract `StepTracker` (~100 lines)
- Extract `RetryTracker` (~100 lines)
- Extract `TimeoutManager` (~150 lines)
- `ExecutionContext` becomes orchestrator (~400 lines)

**Non-goals**:
- No new containment features
- No API changes to `wrap_llm_call()` or `get_graph_snapshot()`
- No changes to `ShieldPipeline`

**Success criteria**:
- `execution_context.py` < 500 lines
- Each tracker has its own test file
- 100% existing test pass (zero behavioral regression)
- No new public API (trackers are internal)

---

### v3.3 -- nogil Python Readiness

**Goal**: Prepare for PEP 703 free-threaded Python (3.14/3.15).

**Scope**:
- Audit all GIL-dependent patterns (T-5, L-6 in V2_DEFERRED.md)
- Add explicit locks where GIL was assumed
- CI matrix entry for `PYTHON_GIL=0`
- `ExecutionGraph` node pruning (`max_nodes` with LRU eviction)

**Non-goals**:
- Not dropping GIL support for current Python versions
- Not rewriting thread-safe code that already uses locks

**Success criteria**:
- All tests pass under `PYTHON_GIL=0` (free-threaded build)
- No performance regression > 5% on standard benchmarks
- `ExecutionGraph` memory bounded with configurable cap

---

### v4.0 -- Federation

**Goal**: Cross-process/cross-org budget coordination.

**Scope**:
- `FederationNode` with cryptographic identity
- `BudgetGrant` with time-limited, policy-constrained delegation
- `FederationGateway` (HTTP/gRPC transport, extras dependency)
- mTLS mutual authentication
- Grant lifecycle: request -> approve -> use -> report -> expire/revoke

**Non-goals**:
- No agent routing or orchestration
- No service mesh integration
- No real-time streaming between nodes

**Success criteria**:
- Two VERONICA instances share budget across processes
- Grant expires on timeout (pessimistic model)
- Network partition does not cause double-spend
- `pip install veronica-core[federation]` installs transport deps

---

### v4.1 -- Adapter Ecosystem Hardening

**Goal**: Stabilize adapter SPI. Reduce per-adapter maintenance burden.

**Scope**:
- Generic adapter test harness (parameterized)
- Adapter version declaration (min/max framework versions)
- Scaffold tool (`veronica new-adapter`)
- Extract `_shared.py` duplication into composable mixins

**Non-goals**:
- No new framework adapters in this milestone
- No breaking adapter API changes without migration period

**Success criteria**:
- All 8 adapters pass shared test harness
- New adapter scaffold produces working adapter in < 30 minutes
- CI tests each adapter against min and max supported framework version

---

### v4.2 -- Memory Governance

**Goal**: Extend containment to memory system operations.

**Scope**:
- `MemoryBoundaryHook` for ShieldPipeline
- `ToolCallContext.kind` extended with `memory_read` / `memory_write`
- Trust-based memory isolation via `TrustBasedPolicyRouter`
- Memory cost tracking toward budget

**Non-goals**:
- No specific memory system implementation (TriMemory, Mem0, etc.)
- No vector DB client

**Success criteria**:
- Memory read/write counted toward budget
- UNTRUSTED agents blocked from TRUSTED memory
- At least one integration example with a real memory system

---

## 2. Issues

### v3.1 Issues

#### `[v3.1] Lazy imports for __init__.py`

**Description**: Replace 140 eager imports with `__getattr__`-based lazy loading. Group exports into sub-namespaces.

**Technical background**: `__init__.py` currently has 140 top-level symbols across 25 import blocks. Every `import veronica_core` loads all of them. For a library that users may only use 5-10 symbols, this is wasteful and noisy.

**Implementation tasks**:
1. Define namespace tiers:
   - `veronica_core` (core): `ExecutionContext`, `ExecutionConfig`, `WrapOptions`, `veronica_guard`, `init`, `shutdown`, `get_context`, `patch_openai`, `patch_anthropic`
   - `veronica_core.shield`: `ShieldConfig`, `BudgetWindowHook`, `TokenBudgetHook`, `AdaptiveBudgetHook`, `TimeAwarePolicy`, all config classes
   - `veronica_core.distributed`: `BudgetBackend`, `RedisBudgetBackend`, `DistributedCircuitBreaker`, `LocalBudgetBackend`
   - `veronica_core.adapters`: `CircuitBreakerCapability`, `MCPContainmentAdapter`, `AsyncMCPContainmentAdapter`
   - `veronica_core.a2a`: `TrustLevel`, `AgentIdentity`, `TrustBasedPolicyRouter`, `TrustEscalationTracker`
   - `veronica_core.simulation`: already exists
   - `veronica_core.compliance`: `ComplianceExporter`, `AuditChain`, `AuditEntry`
   - `veronica_core.otel_feedback`: already exists
2. Implement `__getattr__` in `__init__.py` for peripheral symbols
3. Emit `DeprecationWarning` for symbols accessed via top-level that moved to sub-namespaces
4. Keep `__all__` updated (only core symbols)
5. Update all examples and tests to use new import paths
6. Measure import time before/after

**Acceptance criteria**:
- `python -X importtime -c "import veronica_core"` shows < 50% of current time
- `from veronica_core import ExecutionContext` works
- `from veronica_core import AdaptiveBudgetHook` emits DeprecationWarning
- `from veronica_core.shield import AdaptiveBudgetHook` works without warning
- All existing tests pass

**Risk**: `isinstance()` checks in user code may break if lazy import returns different module reference. Test with all adapters. Test with `pickle` serialization.

---

#### `[v3.1] Unify adapter/ and adapters/ directories`

**Description**: Move `adapter/exec.py` (278 lines) into `adapters/`. One directory for all adapter code.

**Implementation tasks**:
1. Move `adapter/exec.py` -> `adapters/exec.py`
2. Update all internal imports
3. Add backward-compat shim: `adapter/__init__.py` with `from adapters.exec import *` + `DeprecationWarning`
4. Remove `adapter/` directory after 1 minor release cycle
5. Update `docs/architecture.md`

**Acceptance criteria**:
- `adapter/` directory empty (or shim only)
- All imports resolve
- All tests pass

---

#### `[v3.1] time.monotonic() migration`

**Description**: Replace `time.time()` with `time.monotonic()` in timeout paths. `time.time()` is affected by NTP clock adjustments and DST. `time.monotonic()` is not.

**Implementation tasks**:
1. `execution_context.py`: Replace all `time.time()` in timeout logic
2. `circuit_breaker.py`: Replace in cooldown timer
3. `distributed_circuit_breaker.py`: Replace in local timeout paths (Redis TTLs are server-side, unaffected)
4. Search for any other `time.time()` usage in non-log contexts
5. Add test: mock `time.monotonic` to verify timeout behavior

**Acceptance criteria**:
- `grep -rn "time\.time()" src/veronica_core/` returns zero hits outside logging/timestamp contexts
- Timeout test passes with mocked monotonic clock
- No behavioral regression

---

#### `[v3.1] Shared test fixtures (conftest.py)`

**Description**: 157 test files duplicate fixture patterns. Extract into shared `conftest.py`.

**Implementation tasks**:
1. Audit test files for common patterns:
   - `_make_ctx()` / `_make_config()` -> `@pytest.fixture`
   - `_make_pipeline()` -> `@pytest.fixture`
   - `_make_agent()` / `_make_cb()` -> `@pytest.fixture`
   - Fake Redis client -> shared fixture
2. Create `tests/conftest.py` with parameterized fixtures
3. Update 20+ test files that use the most common patterns
4. Do NOT migrate all 157 files at once -- do the top 20 by frequency, leave rest for follow-up

**Acceptance criteria**:
- `tests/conftest.py` exists with 5+ fixtures
- At least 20 test files updated to use shared fixtures
- All tests pass
- No new test failures from fixture scope changes

---

#### `[v3.1] persist module test backfill`

**Description**: `persist.py` has 5 inline tests. For a persistence module in a safety-critical library, this is insufficient.

**Implementation tasks**:
1. Create `tests/test_persist.py`
2. Cover: save/load cycle, atomic write (tmp -> rename), concurrent writes, corrupted file recovery, empty state, large state, cross-platform path handling
3. Adversarial: SIGKILL mid-write simulation (verify tmp file does not corrupt state), concurrent save/load race

**Acceptance criteria**:
- 15+ tests in `test_persist.py`
- Covers happy path, error cases, concurrent access
- Coverage for `persist.py` > 90%

---

#### `[v3.1] BudgetEnforcer limit_usd=0.0 edge case`

**Description**: `BudgetEnforcer` with `limit_usd=0.0` returns `inf` utilization. Should return 100%.

**Implementation tasks**:
1. Add `if self._limit_usd == 0.0: return 1.0` guard in utilization calculation
2. Add test for `limit_usd=0.0`

**Acceptance criteria**:
- `BudgetEnforcer(limit_usd=0.0).utilization` returns `1.0`
- `BudgetEnforcer(limit_usd=0.0).check()` returns HALT

---

### v3.2 Issues

#### `[v3.2] Extract BudgetTracker from ExecutionContext`

**Description**: Move cost accumulation, budget checking, reserve/commit/rollback lifecycle into `BudgetTracker` class.

**Implementation tasks**:
1. Create `containment/budget_tracker.py`
2. Extract: `_cost_usd_accumulated`, `_reserved_cost`, budget check logic, reserve/commit/rollback methods
3. `ExecutionContext.__init__` creates `self._budget = BudgetTracker(...)`
4. Delegate all budget operations
5. `BudgetTracker` exposes same thread-safety guarantees (lock held during check-and-update)
6. Create `tests/test_budget_tracker.py`

**Acceptance criteria**:
- `BudgetTracker` independently testable
- `ExecutionContext` delegates all budget ops to `BudgetTracker`
- All existing tests pass without modification
- `execution_context.py` reduced by ~300 lines

---

#### `[v3.2] Extract StepTracker from ExecutionContext`

Same pattern as BudgetTracker. Step counting, max_steps enforcement.

---

#### `[v3.2] Extract RetryTracker from ExecutionContext`

Same pattern. Retry counting, retry policy evaluation.

---

#### `[v3.2] Extract TimeoutManager from ExecutionContext`

Same pattern. Timeout enforcement using `time.monotonic()` (from v3.1).

---

### v3.3 Issues

#### `[v3.3] Audit GIL-dependent patterns`

**Description**: Identify all code paths that rely on CPython GIL for atomicity.

**Implementation tasks**:
1. Search for patterns: attribute read-then-write without lock, dict operations assumed atomic, list append assumed atomic
2. Document each finding with file:line and remediation
3. Cross-reference with V2_DEFERRED.md items T-5, L-6

**Acceptance criteria**:
- Audit document listing all GIL-dependent patterns
- Each pattern has a remediation plan
- Estimated lock count increase documented

---

#### `[v3.3] Add locks for GIL-dependent paths`

**Description**: Add explicit `threading.Lock` where GIL atomicity was assumed.

Depends on: `[v3.3] Audit GIL-dependent patterns`

**Implementation tasks**:
1. `CircuitBreaker.record_failure()`: predicate evaluation inside lock
2. `BudgetEnforcer`: limit check-and-update atomic
3. `ComplianceExporter._attached`: protected by lock
4. Each added lock must have a corresponding test with `PYTHON_GIL=0` CI variant

**Acceptance criteria**:
- All identified GIL-dependent paths protected
- Tests pass under `PYTHON_GIL=0`
- Benchmark: < 5% regression on standard workload

---

#### `[v3.3] ExecutionGraph node pruning`

**Description**: Add `max_nodes` config to `ExecutionGraph`. LRU eviction for long-running contexts.

**Implementation tasks**:
1. Add `max_nodes: int = 0` to `ExecutionGraph.__init__` (0 = unlimited, backward compat)
2. When `len(self._nodes) > max_nodes`, evict oldest completed nodes
3. Never evict in-progress nodes
4. Add test: 10K nodes with `max_nodes=100` -- verify memory bounded

**Acceptance criteria**:
- Default behavior unchanged (`max_nodes=0`)
- Memory bounded when configured
- Eviction does not affect in-progress nodes

---

### v4.0 Issues

#### `[v4.0] FederationNode identity`

Process-level identity with ed25519 key pair. Reuse `PolicySignerV2` infrastructure.

#### `[v4.0] BudgetGrant protocol`

Time-limited, policy-constrained budget delegation. Dataclass with grantor, grantee, amount, expiry, attached ShieldPipeline constraints.

#### `[v4.0] FederationGateway transport`

HTTP/gRPC transport layer. Extras dependency. mTLS mutual authentication. Grant lifecycle endpoints.

#### `[v4.0] Partition-safe grant model`

Pessimistic grants: grantee cannot exceed granted amount even if grantor unreachable. Grants auto-expire. No split-brain double-spend.

---

### v4.1 Issues

#### `[v4.1] Adapter test harness`

Generic parameterized test suite. Any adapter class plugs in. Tests: cost extraction, HALT propagation, DEGRADE behavior, async support, error handling.

#### `[v4.1] Adapter version declaration`

Each adapter declares supported framework version range. CI matrix tests min and max.

#### `[v4.1] Adapter scaffold generator`

`veronica new-adapter --framework=X` generates boilerplate.

---

### v4.2 Issues

#### `[v4.2] MemoryBoundaryHook`

New hook type for ShieldPipeline. Controls read/write to agent memory stores.

#### `[v4.2] ToolCallContext.kind extension`

Add `memory_read` / `memory_write` to `kind` field. Currently only `llm` and `tool`.

#### `[v4.2] Trust-based memory isolation`

UNTRUSTED agents cannot read TRUSTED memory. Enforced via TrustBasedPolicyRouter.

---

## 3. Execution Order

```
 1. [v3.1] time.monotonic() migration
 2. [v3.1] Unify adapter/ and adapters/
 3. [v3.1] BudgetEnforcer limit_usd=0.0 fix
 4. [v3.1] Shared test fixtures (conftest.py)
 5. [v3.1] persist module test backfill
 6. [v3.1] Lazy imports for __init__.py
 7. [v3.2] Extract BudgetTracker
 8. [v3.2] Extract StepTracker
 9. [v3.2] Extract RetryTracker
10. [v3.2] Extract TimeoutManager
11. [v3.3] Audit GIL-dependent patterns
12. [v3.3] Add locks for GIL-dependent paths
13. [v3.3] ExecutionGraph node pruning
14. [v3.3] CI: PYTHON_GIL=0 matrix entry
15. [v4.0] FederationNode identity
16. [v4.0] BudgetGrant protocol
17. [v4.0] FederationGateway transport
18. [v4.0] Partition-safe grant model
19. [v4.1] Adapter test harness
20. [v4.1] Adapter version declaration
21. [v4.1] Adapter scaffold generator
22. [v4.2] MemoryBoundaryHook
23. [v4.2] ToolCallContext.kind extension
24. [v4.2] Trust-based memory isolation
```

**Why this order**:

- `time.monotonic()` first because it is a correctness fix. Every subsequent change builds on correct timeout behavior.
- `adapter/` unification and `limit_usd=0.0` are small, independent. Ship them early to reduce open debt.
- `conftest.py` and `persist` test backfill before lazy imports because lazy imports will touch all test files -- having shared fixtures reduces the blast radius.
- Lazy imports last in v3.1 because it is the highest-risk change and benefits from all other v3.1 cleanup being done first.
- v3.2 trackers are sequential because each extraction narrows `execution_context.py` and makes the next extraction easier. BudgetTracker first because it is the largest (~300 lines).
- v3.3 audit before locks -- you cannot add locks without knowing where they are needed.
- v4.0 federation depends on v3.1-3.3 kernel stability. No point federating an unstable kernel.
- v4.1 adapter hardening depends on v3.2 (clean internal API) and benefits from v3.1 namespace cleanup.
- v4.2 memory governance is last because it requires a concrete memory system to integrate with. Build when demand exists.

---

## 4. PR Strategy

### Branch model

```
main                          production, tagged releases
  |
  +-- v3.1/lazy-imports       one branch per issue
  +-- v3.1/monotonic-migration
  +-- v3.1/adapter-unify
  +-- v3.2/budget-tracker
  +-- v3.2/step-tracker
  ...
```

No long-lived `dev` branch. Each issue gets a feature branch off `main`. PRs merge to `main` directly.

### PR size

- Target: < 500 lines changed per PR
- Hard limit: 1000 lines (except for v3.1 lazy imports which will touch many files)
- If a PR exceeds 500 lines, split into preparatory PR (refactor/test) + implementation PR

### Backward compatibility

- v3.1: All existing import paths work. New namespaces are additive. Peripheral symbols emit `DeprecationWarning` at top level. Warnings become errors in v4.0.
- v3.2: No public API changes. `ExecutionContext` external interface unchanged. Trackers are internal (`_budget`, `_steps`, etc.).
- v3.3: No API changes. Lock additions are internal. `max_nodes` parameter is opt-in with default 0 (unlimited).
- v4.0: New `[federation]` extras. No changes to core API.
- v4.1: Adapter SPI may add required methods. Migration period: 1 minor version with deprecation warnings before removal.

### Feature flags

Not needed for v3.1-3.3 (all changes are internal or additive).

For v4.0 federation: `pip install veronica-core[federation]` is the feature flag. Core package does not include transport dependencies.

### Migration strategy

v3.1 import changes:
```python
# Before (works, emits DeprecationWarning in v3.1, removed in v4.0)
from veronica_core import AdaptiveBudgetHook

# After (preferred)
from veronica_core.shield import AdaptiveBudgetHook
```

Migration script: `scripts/migrate_imports.py` -- sed-based rewriter for user codebases.

---

## 5. Testing Plan

### Per-milestone requirements

#### v3.1

- All 4045+ existing tests pass (zero regression)
- New `test_persist.py`: 15+ tests
- Import time benchmark: before/after comparison in CI log
- `DeprecationWarning` tests: verify warnings fire for old import paths

#### v3.2

- All existing tests pass WITHOUT modification (trackers are internal)
- New test files: `test_budget_tracker.py`, `test_step_tracker.py`, `test_retry_tracker.py`, `test_timeout_manager.py`
- Each tracker: 20+ tests covering happy path, boundary, concurrent access
- Integration test: full `wrap_llm_call()` flow exercises all trackers together

#### v3.3

- `PYTHON_GIL=0` CI matrix entry
- Concurrent stress tests: 100 threads hitting shared `CircuitBreaker`, `BudgetEnforcer`, `ExecutionGraph`
- Benchmark: wall-clock comparison with/without added locks
- Node pruning: memory usage test with 100K nodes and `max_nodes=1000`

#### v4.0

- Integration test: two VERONICA instances on localhost
- Grant lifecycle: request -> approve -> use -> expire
- Partition test: kill network between nodes, verify no double-spend
- mTLS: invalid cert rejected, expired cert rejected, revoked cert rejected

#### v4.1

- Adapter test harness: parameterized test class, all 8 adapters pass
- Scaffold: generated adapter passes harness without modification
- Version matrix: CI tests each adapter against declared min/max framework version

#### v4.2

- Memory boundary: UNTRUSTED agent blocked from TRUSTED memory
- Memory cost: retrieval counted toward budget, HALT when exceeded
- Integration: at least one real memory system (e.g. in-memory mock with TriMemory interface)

### Test structure

```
tests/
  conftest.py                    # shared fixtures (v3.1)
  test_persist.py                # persist module (v3.1)
  test_budget_tracker.py         # extracted tracker (v3.2)
  test_step_tracker.py           # extracted tracker (v3.2)
  test_retry_tracker.py          # extracted tracker (v3.2)
  test_timeout_manager.py        # extracted tracker (v3.2)
  test_nogil_*.py                # free-threaded tests (v3.3)
  test_federation_*.py           # federation tests (v4.0)
  test_adapter_harness.py        # shared adapter tests (v4.1)
  test_memory_boundary.py        # memory governance (v4.2)
```

### Failure injection

- v3.3: Thread contention via `threading.Barrier` to force race conditions
- v4.0: Network partition simulation via socket timeout injection
- v4.0: Clock skew simulation for grant expiry edge cases
- v4.1: Adapter framework mock that raises in unexpected places

---

## 6. Architecture Guardrails

Rules for all contributions. Violations block merge.

### G-1: Kernel determinism

Every decision path through `ExecutionContext` and `ShieldPipeline` MUST return a `Decision` enum value (ALLOW, DEGRADE, HALT). No advisory-only modes. No "log and continue" paths. If the kernel evaluates a call, the result is binding.

### G-2: Model independence

No component in `src/veronica_core/` may inspect, parse, modify, or route based on LLM prompt or completion content. VERONICA governs execution resources (cost, steps, retries, time), not content. Content inspection is the application's responsibility.

### G-3: Fail-closed policy execution

If a `ShieldPipeline` hook raises an unhandled exception during evaluation, the decision MUST be HALT (not ALLOW). Exceptions in the governance path are containment failures, not pass-through events.

### G-4: Adapter isolation

Framework adapters (`adapters/*.py`) MUST NOT modify kernel state directly. They interact with `ExecutionContext` through the public API (`wrap_llm_call`, `record_cost`, `get_graph_snapshot`). No adapter may reach into `_budget`, `_steps`, or other internal tracker state.

### G-5: Zero required dependencies

`pyproject.toml` MUST have zero entries in `[project.dependencies]`. All external packages (Redis, OTel, Vault, gRPC) MUST be in `[project.optional-dependencies]` under named extras.

### G-6: Protocol-first extension

New extension points MUST be defined as `@runtime_checkable` `Protocol` classes in `protocols.py`. Concrete implementations import from protocols, not the other way around. This prevents import cycles and ensures replaceability.

### G-7: Backward-compatible releases

Minor versions (3.x) MUST NOT remove public API symbols. Deprecation cycle: emit `DeprecationWarning` for one minor version, remove in next major version.

### G-8: Thread safety documentation

Every class that holds mutable state MUST document its thread-safety guarantees in the class docstring. Options: "thread-safe" (all methods safe for concurrent use), "not thread-safe" (caller must synchronize), or "thread-safe with caveats" (specific methods documented).
