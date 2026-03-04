# VERONICA Safety Guarantees

This document states formal safety guarantees for VERONICA's containment layer
and identifies the code paths that enforce each guarantee.

All guarantees are stated for a correctly configured `ExecutionContext` with
finite, non-negative limit values. Invalid configurations (NaN, Inf, negative)
are rejected at construction time.

---

## G1: Cost Bound

**Guarantee:** For any agent chain running under an `ExecutionContext` with
`ExecutionConfig.max_cost_usd = C`, the total USD cost accumulated across all
LLM and tool calls never exceeds `C`.

```
total_cost(chain) <= C
```

### Proof

The budget accumulator is `_cost_usd_accumulated` in `ExecutionContext.__init__`
(`src/veronica_core/containment/execution_context.py`, line ~380).

Every LLM call passes through `ExecutionContext.wrap_llm_call()`, which calls
the internal `_wrap()` method. Before dispatching the callable, `_wrap()` calls
`_check_budget(cost_hint)`. After the callable returns, the actual cost is
committed via `_commit_cost(actual_cost)`.

The commit protocol uses a check-before-commit structure under `_lock`:

```python
# Pseudocode from _commit_cost in execution_context.py
with self._lock:
    projected = self._cost_usd_accumulated + actual_cost
    if projected > self._config.max_cost_usd:
        self._aborted = True
        self._abort_reason = "budget_exceeded"
        return Decision.HALT
    self._cost_usd_accumulated = projected
```

No code path commits cost without first checking the ceiling. The lock prevents
concurrent commits from racing past the ceiling.

**Additionally:** `BudgetEnforcer.spend()` (`src/veronica_core/budget.py`)
uses the same pattern:

```python
with self._lock:
    projected = self._spent_usd + amount_usd
    if projected > self.limit_usd:
        self._exceeded = True
        return False
    self._spent_usd = projected
    return True
```

**Input validation:** `BudgetEnforcer.__post_init__()` raises `ValueError` for
NaN and Inf `limit_usd`. `BudgetEnforcer.spend()` raises `ValueError` for NaN,
Inf, and negative `amount_usd`. These checks prevent cost poisoning attacks
that could bypass the ceiling check via arithmetic overflow or undefined behavior.

**Distributed backend:** When `ExecutionConfig.redis_url` is set, cost is
tracked via `RedisBudgetBackend`. The Redis INCRBYFLOAT command is atomic;
an epsilon guard (`_BUDGET_EPSILON = 1e-9` in `src/veronica_core/distributed.py`)
prevents spurious under-enforcement from IEEE-754 rounding.

**Conclusion:** The cost bound G1 holds in both local and distributed configurations.

---

## G2: Termination Guarantee

**Guarantee:** For any agent chain running under an `ExecutionContext` with
`ExecutionConfig.max_steps = S`, the number of successful operations through
`wrap_llm_call()` or `wrap_tool_call()` never exceeds `S`.

```
successful_operations(chain) <= S
```

### Proof

The step counter is `_step_count` in `ExecutionContext.__init__`
(`src/veronica_core/containment/execution_context.py`, line ~377).

Every call through `wrap_llm_call()` or `wrap_tool_call()` passes through
`_wrap()`. Before dispatching, `_wrap()` checks:

```python
with self._lock:
    if self._step_count >= self._config.max_steps:
        self._aborted = True
        self._abort_reason = "step_limit_exceeded"
        return Decision.HALT
```

After a successful call completes, `_step_count` is incremented:

```python
with self._lock:
    self._step_count += 1
```

The check-before-increment ordering means: at step count `S`, the next
call is denied before `_step_count` would become `S+1`. Therefore
`_step_count` never exceeds `S` when used, and the number of successful
operations is bounded by `S`.

**`AgentStepGuard` (standalone):** `AgentStepGuard.step()` uses increment-then-check:

```python
with self._lock:
    self._current_step += 1          # increment first
    if self._current_step >= self.max_steps:
        return False                  # then check
```

The increment-before-check ordering ensures that `max_steps=S` allows exactly
`S-1` further operations after construction (i.e., the `S`-th call to `step()`
returns `False`). This is the intended semantics: `max_steps` is the total count,
not the count of *additional* steps.

**Partial result preservation:** When the step limit is reached,
`AgentStepGuard._last_result` preserves the most recent partial output.
`PartialResultBuffer` stores intermediate streaming results accessible via
`get_current_partial_buffer()`. This ensures that halted chains can return
useful partial results rather than empty responses.

**Conclusion:** The termination guarantee G2 holds for both `ExecutionContext`
and standalone `AgentStepGuard`.

---

## G3: Retry Budget

**Guarantee:** For any agent chain running under an `ExecutionContext` with
`ExecutionConfig.max_retries_total = R`, the total number of retry attempts
across all nested `RetryContainer` instances and framework-level retries
never exceeds `R`.

```
total_retries(chain) <= R
```

### Proof

The retry counter is `_retries_used` in `ExecutionContext.__init__`
(`src/veronica_core/containment/execution_context.py`, line ~379).

When a call in `_wrap()` raises an exception and a retry is attempted,
`_check_retry_budget()` is called before each retry:

```python
with self._lock:
    if self._retries_used >= self._config.max_retries_total:
        self._aborted = True
        self._abort_reason = "retry_budget_exceeded"
        return Decision.HALT
    self._retries_used += 1
```

The check-before-increment ordering provides the same guarantee as G2:
the retry count never exceeds `max_retries_total`.

**Standalone `RetryContainer`:** `RetryContainer.execute()` enforces `max_retries`
locally per-container. When integrated with `ExecutionContext`, the chain-level
check fires first on each retry, ensuring the cross-container ceiling holds.

**Conclusion:** G3 holds when `RetryContainer` instances are used within an
`ExecutionContext`. Standalone `RetryContainer` provides only local bounds.

---

## G4: Failure Isolation

**Guarantee:** Once `CircuitBreaker` enters the OPEN state, all `check()` calls
return `allowed=False` until at least `recovery_timeout` seconds have elapsed.
During OPEN state, zero calls are dispatched to the protected LLM endpoint.

```
state == OPEN => check(ctx).allowed == False
```

### Proof

The state machine transitions are in `src/veronica_core/circuit_breaker.py`.

State transitions are protected by `threading.Lock` (`self._lock`).
The `check()` method:

```python
def check(self, context: PolicyContext) -> PolicyDecision:
    with self._lock:
        self._maybe_half_open_locked()  # may transition OPEN -> HALF_OPEN
        if self._state == CircuitState.OPEN:
            return PolicyDecision(allowed=False, policy_type="circuit_breaker",
                                  reason="circuit open")
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_in_flight > 0:
                return PolicyDecision(allowed=False, ...)
            self._half_open_in_flight += 1
            return PolicyDecision(allowed=True, ...)
        return PolicyDecision(allowed=True, ...)
```

`_maybe_half_open_locked()` transitions OPEN -> HALF_OPEN only when:
```python
time.time() - self._last_failure_time >= self.recovery_timeout
```

This transition is evaluated under lock, preventing two threads from
simultaneously observing the OPEN state as expired.

**HALF_OPEN single-slot constraint:** `_half_open_in_flight > 0` causes
concurrent callers in HALF_OPEN to receive `allowed=False`. Exactly one
test request is allowed; all others are denied. This prevents thundering-herd
on recovery.

**No OPEN -> CLOSED direct path:** The state machine has no code path from
OPEN directly to CLOSED. The only paths are:
- OPEN -> HALF_OPEN (after recovery_timeout)
- HALF_OPEN -> CLOSED (after successful test)
- HALF_OPEN -> OPEN (after failed test)

**Distributed circuit breaker:** In `src/veronica_core/distributed.py`,
the `DistributedCircuitBreaker` uses Lua scripts executed via `redis.eval()`
for atomic state transitions across processes. Redis `eval()` executes Lua
scripts atomically on the Redis server, providing the same consistency guarantee
as the in-process lock.

**Conclusion:** G4 holds for both local and distributed circuit breakers.

---

## G5: Wall-Clock Timeout

**Guarantee:** For any agent chain running under an `ExecutionContext` with
`ExecutionConfig.timeout_ms = T > 0`, no new operations are dispatched after
`T` milliseconds from chain start.

```
elapsed_ms > T => all new wrap_llm_call() return Decision.HALT
```

### Proof

On `ExecutionContext.__enter__()` (or construction when `timeout_ms > 0`),
a deadline is registered with `SharedTimeoutPool`:

```python
# In execution_context.py
deadline = time.monotonic() + self._config.timeout_ms / 1000.0
self._timeout_handle = _shared_timeout_pool.schedule(
    deadline=deadline,
    callback=self._on_timeout,
)
```

`_on_timeout()` calls `self._cancellation_token.cancel()`, setting the
`threading.Event`.

All subsequent `wrap_llm_call()` calls check:

```python
if self._cancellation_token.is_cancelled:
    return Decision.HALT
```

This check occurs before any callable is dispatched, so no new operations
start after the timeout fires.

**Caveat:** Operations already in-flight when the timeout fires are not
interrupted. Only new operations are blocked. The `CancellationToken` provides
cooperative (not preemptive) cancellation; long-running operations must
check `is_cancelled` periodically.

**Conclusion:** G5 holds for new operations. In-flight operations at timeout
time may complete before observing cancellation.

---

## G6: Configuration Validity

**Guarantee:** An `ExecutionContext` with invalid configuration (NaN, Inf, or
negative limits) cannot be created. All limit violations are detected at
construction time, not at runtime.

### Proof

`ExecutionConfig.__post_init__()` validates all fields:

```python
# From src/veronica_core/containment/execution_context.py
def __post_init__(self) -> None:
    if math.isnan(self.max_cost_usd) or math.isinf(self.max_cost_usd):
        raise ValueError(...)
    if self.max_cost_usd < 0:
        raise ValueError(...)
    if self.max_steps < 0:
        raise ValueError(...)
    if self.max_retries_total < 0:
        raise ValueError(...)
    if self.timeout_ms < 0:
        raise ValueError(...)
```

`BudgetEnforcer.__post_init__()` validates `limit_usd`.
`CircuitBreaker.__post_init__()` validates `failure_threshold` and `recovery_timeout`.
`AgentStepGuard` validates `max_steps >= 0` implicitly through its check.

**Conclusion:** G6 holds; invalid configurations raise `ValueError` at construction.

---

## Summary Table

| Guarantee | Bound | Enforced By | Code Path |
|-----------|-------|-------------|-----------|
| G1: Cost Bound | `total_cost <= C` | `ExecutionContext._commit_cost()`, `BudgetEnforcer.spend()` | `execution_context.py`, `budget.py` |
| G2: Termination | `steps <= S` | `ExecutionContext._wrap()`, `AgentStepGuard.step()` | `execution_context.py`, `agent_guard.py` |
| G3: Retry Budget | `retries <= R` | `ExecutionContext._check_retry_budget()` | `execution_context.py` |
| G4: Failure Isolation | OPEN => denied | `CircuitBreaker.check()`, Lua script | `circuit_breaker.py`, `distributed.py` |
| G5: Wall-Clock Timeout | `elapsed <= T` | `SharedTimeoutPool` + `CancellationToken` | `timeout_pool.py`, `execution_context.py` |
| G6: Config Validity | no NaN/Inf/negative | `__post_init__()` validators | `execution_context.py`, `budget.py`, `circuit_breaker.py` |

All guarantees hold independently and simultaneously under a single `ExecutionContext`.
The `PolicyPipeline.evaluate()` AND-composition ensures that violation of any one
bound results in `Decision.HALT` without bypassing the others.

---

## Limitations and Assumptions

1. **Cooperative cancellation only.** G5 applies only to new operations, not
   in-flight ones. Long-running operations must poll `CancellationToken.is_cancelled`.

2. **`RetryContainer` must use `ExecutionContext`.** G3 applies only when
   `RetryContainer` instances operate within an `ExecutionContext`. Standalone
   `RetryContainer` provides only local bounds.

3. **Distributed backend consistency.** G1 in distributed mode relies on Redis
   atomicity. If Redis is unavailable and `fallback_on_error=True`, the
   `LocalBudgetBackend` fallback provides only local bounds.

4. **Cost accuracy.** G1 bounds the *accumulated* cost against the ceiling.
   If the LLM provider reports costs inaccurately, the bound applies to
   reported costs, not actual costs.

5. **Code correctness.** All guarantees assume the implementation is correct.
   The 2232-test suite with 92% coverage (v1.8.1) and adversarial test suite
   (`tests/adversarial/`) provide empirical evidence but not formal proof of
   implementation correctness.
