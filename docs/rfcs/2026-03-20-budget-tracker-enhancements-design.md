# RFC: BudgetTracker Enhancements -- Token Limits, Time Windows, Composite Scopes

**Date:** 2026-03-20
**Status:** Draft (R4)
**Author:** amabito

---

## Motivation

veronica-core's budget system currently tracks cumulative USD cost per-run.
An external contribution to agent-control (#130) requires a BudgetStore
protocol with time-windowed, multi-axis, token+USD tracking. This RFC
designs three enhancements so veronica-core can implement that protocol
via an adapter without breaking the existing API.

---

## Current State

**BudgetTracker** (`_budget_tracker.py`, internal, not exported):
- Single `float _cost` + `threading.Lock()`
- Methods: `add(amount)`, `add_returning(amount)`, `check(max_cost, epsilon)`, `set(value)`
- Property: `cost -> float`
- No token tracking, no time windows, no scoping

**BudgetEnforcer** (`budget.py`, public, `@dataclass`):
- Field: `limit_usd: float` (dataclass field, not constructor param)
- `spend(amount_usd) -> bool`, `check(context) -> PolicyDecision`
- Properties: `spent_usd`, `remaining_usd`, `is_exceeded`, `utilization`, `call_count`
- Implements `RuntimePolicy` protocol
- Owns its own `_lock: threading.Lock()` and `_spent_usd: float` internally
- **Does NOT delegate to BudgetTracker** -- independent implementation

**ExecutionConfig** (`containment/types.py`, `@dataclass(frozen=True)`):
- `max_cost_usd: float` -- single USD ceiling
- Field order: `max_cost_usd`, `max_steps`, `max_retries_total`, `timeout_ms=0`, `budget_backend=None`, `redis_url=None`
- No token limits, no time window, no scope config

**_LimitChecker** (`containment/_limit_checker.py`, internal):
- Composes `BudgetTracker`, `StepTracker`, `RetryTracker`, `TimeoutManager`
- `check_limits(budget_backend, emit_fn) -> str | None`
- Reads `self.budget.cost` inline, does NOT call `BudgetTracker.check()`
- Calls `emit_fn(stop_reason, reason)` for each limit violation

**ExecutionContext** (`containment/execution_context.py`):
- Owns `_LimitChecker` which owns `BudgetTracker`
- One `BudgetTracker` per context, flat, per-request lifecycle

**Adapters** (`adapters/_shared.py`):
- `record_budget_spend(container, cost, tag, logger)` -- USD only
- `_BudgetProxy` reads/writes cost via `_add_cost_returning()`

**Key architecture point:** BudgetEnforcer and BudgetTracker are two
independent implementations. BudgetEnforcer does NOT delegate to
BudgetTracker. They serve different use cases:
- BudgetTracker: internal to ExecutionContext, per-request
- BudgetEnforcer: public API, standalone, can be long-lived

---

## Enhancement 1: Token-Based Limits

### Scope: Small

### Public API changes

**ExecutionConfig** -- add optional token limit at the END (after all
non-default fields to avoid dataclass field ordering error):

```python
@dataclass(frozen=True)
class ExecutionConfig:
    max_cost_usd: float
    max_steps: int
    max_retries_total: int
    timeout_ms: int = 0
    budget_backend: Any | None = None
    redis_url: str | None = None
    max_tokens: int | None = None       # NEW: combined token ceiling
```

Validation in `__post_init__`: `max_tokens` must be `None` or non-negative
finite integer.

**BudgetEnforcer** -- add dataclass fields and extend spend():

```python
@dataclass
class BudgetEnforcer:
    limit_usd: float = 100.0
    limit_tokens: int | None = None     # NEW dataclass field

    def spend(
        self,
        amount_usd: float,
        input_tokens: int = 0,          # NEW, backward-compatible
        output_tokens: int = 0,         # NEW, backward-compatible
    ) -> bool:
        """Record spending. Returns True if within budget."""
        # Validate: input_tokens >= 0, output_tokens >= 0, both int
        with self._lock:
            self._spent_usd += amount_usd
            self._input_tokens += input_tokens    # NEW
            self._output_tokens += output_tokens  # NEW
            return (
                self._spent_usd <= self.limit_usd
                and (self.limit_tokens is None
                     or self._input_tokens + self._output_tokens <= self.limit_tokens)
            )
```

New properties:

```python
@property
def spent_tokens(self) -> int:
    """Total input + output tokens spent."""
    with self._lock:
        return self._input_tokens + self._output_tokens

@property
def remaining_tokens(self) -> int | None:
    """Remaining token budget, or None if no token limit."""
    if self.limit_tokens is None:
        return None
    return max(0, self.limit_tokens - self.spent_tokens)
```

**ContextSnapshot** -- add token field (at end, with default):

```python
@dataclass(frozen=True)
class ContextSnapshot:
    # ... existing fields unchanged ...
    tokens_accumulated: int = 0          # NEW
```

Note: adding a defaulted field at the end does not affect equality of
existing ContextSnapshot instances (they would compare equal if the new
field matches the default).

### Internal changes

**BudgetTracker** -- add token counters:

```python
class BudgetTracker:
    def __init__(self) -> None:
        self._cost: float = 0.0
        self._input_tokens: int = 0      # NEW
        self._output_tokens: int = 0     # NEW
        self._lock = threading.Lock()

    def add(self, amount: float, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._lock:
            self._cost += amount
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens

    def add_returning(self, amount: float, input_tokens: int = 0, output_tokens: int = 0) -> float:
        with self._lock:
            self._cost += amount
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            return self._cost

    @property
    def tokens(self) -> int:
        with self._lock:
            return self._input_tokens + self._output_tokens
```

**_LimitChecker.check_limits()** -- add token check with emit_fn:

```python
def check_limits(self, budget_backend, emit_fn) -> str | None:
    # Existing: inline cost check (reads self.budget.cost)
    cost = self.budget.cost
    if cost + _BUDGET_EPSILON >= self._config.max_cost_usd:
        reason = "budget_exceeded"
        emit_fn(reason, f"cost {cost:.4f} >= {self._config.max_cost_usd}")
        return reason

    # NEW: token check
    if self._config.max_tokens is not None:
        tokens = self.budget.tokens
        if tokens >= self._config.max_tokens:
            reason = "token_budget_exceeded"
            emit_fn(reason, f"tokens {tokens} >= {self._config.max_tokens}")
            return reason

    # ... existing step/retry/timeout checks unchanged
```

**Adapter utility** -- `record_budget_spend` needs token-aware variant:

```python
def record_budget_spend(container, cost, tag, logger,
                        input_tokens=0, output_tokens=0):
    """Updated signature. Existing callers pass 0 tokens (backward-compatible)."""
```

Adapters that extract token counts from LLM responses (LangChain
`on_llm_end`, AG2 `on_tool_end`) should pass them through. This is
a separate adapter-update PR per framework, not part of Enhancement 1.

### What breaks

Nothing. All new parameters have backward-compatible defaults.

### Edge cases

- **Zero token limit**: `max_tokens=0` means "no LLM calls allowed."
  Validate in `__post_init__` (non-negative).
- **Negative tokens**: `input_tokens < 0` or `output_tokens < 0` in
  `spend()` raises `ValueError` (same pattern as negative `amount_usd`).
- **Non-integer tokens**: Type-check at runtime. `spend(amount_usd=0.01,
  input_tokens=3.14)` raises `TypeError`.

### Decision: combined limits only for now

Internal tracking splits input/output for future flexibility, but the
limit checks `input + output` total. Separate limits add config
complexity with unclear user demand. Easy to add later.

---

## Enhancement 2: Time Windows

### Scope: Medium (period bucket dict, clock injection, period key derivation)

### Design: time windows live on BudgetEnforcer only

`budget_window` does NOT go on `ExecutionConfig`. `ExecutionConfig` is
per-request and short-lived -- time windows only make sense on a long-lived
`BudgetEnforcer` shared across requests. Per-request contexts use
`ExecutionConfig.max_cost_usd` as a flat ceiling (unchanged).

### Public API changes

**BudgetEnforcer** -- add dataclass fields for window:

```python
@dataclass
class BudgetEnforcer:
    limit_usd: float = 100.0
    limit_tokens: int | None = None
    window: Literal["daily", "weekly", "monthly"] | None = None  # NEW
    clock: Callable[[], datetime] | None = field(default=None, repr=False)  # NEW
    # clock contract:
    #   - MUST be non-blocking (clock() is called BEFORE lock acquisition,
    #     but a slow clock still serializes the calling thread's spend())
    #   - MUST NOT raise (exception propagates to spend() caller)
    #   - MUST return timezone-aware datetime (UTC recommended)
    # Intended for test injection (deterministic timestamps).
    # Default (None) uses datetime.now(timezone.utc).

    def __post_init__(self) -> None:
        # ... existing validation ...
        # NEW: validate window
        valid_windows = {None, "daily", "weekly", "monthly"}
        if self.window not in valid_windows:
            raise ValueError(
                f"window must be one of {valid_windows}, got {self.window!r}"
            )
```

New method:

```python
def current_period_key(self) -> str:
    """Return current period key. Empty string if no window."""
```

### Internal changes

**BudgetEnforcer** owns period key derivation (not BudgetTracker):

```python
def _derive_period_key(self) -> str:
    """Derive period key from current clock time. BudgetEnforcer owns this."""
    if self.window is None:
        return ""
    now = (self.clock or (lambda: datetime.now(timezone.utc)))()
    if self.window == "daily":
        return now.strftime("%Y-%m-%d")
    elif self.window == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"  # Use ISO year, not Gregorian
    elif self.window == "monthly":
        return now.strftime("%Y-%m")
    return ""  # unreachable after validation, but defensive
```

**BudgetEnforcer** internal state uses period buckets:

```python
@dataclass
class _PeriodBucket:
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

# Inside BudgetEnforcer:
# self._buckets: dict[str, _PeriodBucket] = {}
# Key is period_key (e.g. "2026-W12", "2026-03-20", or "")
```

`spend()` derives the period key BEFORE acquiring `_lock`, then
acquires `_lock` for the atomic get-or-create + accumulate:

```python
def spend(self, amount_usd: float, ..., metadata=None) -> bool:
    # Phase 1: derive keys OUTSIDE lock (clock() and key building are I/O-free
    # but keeping them outside the lock is a universal rule -- see Thread Safety)
    period_key = self._derive_period_key()  # calls clock(), no lock held
    scope_key = self._build_scope_key(metadata)  # pure function, no lock needed

    # Phase 2: atomic mutation UNDER lock
    with self._lock:
        bucket = self._scoped_buckets.setdefault(scope_key, {}).setdefault(
            period_key, _PeriodBucket()
        )
        bucket.cost += amount_usd
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        return self._check_bucket(bucket)
```

The TOCTOU window between `_derive_period_key()` and lock acquisition
is bounded by thread scheduling jitter (microseconds). A spend at a
period boundary may be attributed to the just-expired period -- this
is acceptable for daily/weekly/monthly windows.

`spent_usd` and `spent_tokens` return values from the **current**
period's bucket.

**BudgetTracker** receives token counters in Enhancement 1 but is NOT
changed further for Enhancement 2. Time windows are BudgetEnforcer-only.
BudgetTracker remains a simple per-request accumulator inside
ExecutionContext.

### What breaks

Nothing. `window=None` (default) uses a single `""` bucket, identical
to current flat accumulation.

### Edge cases

- **Timezone**: UTC for all period derivation. Users needing local
  timezone inject a custom `clock`.
- **Mid-call rollover**: Cost recorded against the period at `spend()`
  time (call completion). The cost is known only at completion.
- **Memory growth**: One bucket per period. Monthly = 12/year, weekly = 53,
  daily = 366. Acceptable for in-process use. Long-lived services should
  use `budget_backend` for persistence.
- **Concurrent period_key derivation**: `_derive_period_key()` runs
  OUTSIDE `_lock`. Two threads calling `spend()` near midnight: thread A
  derives "2026-03-20", thread B derives "2026-03-21". Both acquire
  `_lock` sequentially and write to their respective period buckets.
  No race -- the bucket lookup + mutation is atomic under `_lock`.

### E2 does not hard-depend on E1

E2 can be implemented with cost-only buckets (`_PeriodBucket.cost`).
Token fields in the bucket are additive. The stated implementation order
(E1 then E2) is a convenience, not a hard dependency.

---

## Enhancement 3: Composite Scope Keys

### Scope: Medium-Large

### Public API changes

**BudgetEnforcer** -- add scope support as dataclass fields:

```python
@dataclass
class BudgetEnforcer:
    limit_usd: float = 100.0
    limit_tokens: int | None = None
    window: Literal["daily", "weekly", "monthly"] | None = None
    scope_keys: tuple[str, ...] = ()    # NEW: metadata fields to aggregate by
    max_scopes: int = 10_000            # NEW: LRU eviction threshold for full buckets
    max_tombstones: int = 100_000       # NEW: cap for eviction tombstones
    clock: Callable[[], datetime] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # ... existing validation ...
        # NEW: validate scope_keys
        for i, sk in enumerate(self.scope_keys):
            if not sk or "|" in sk or "=" in sk:
                raise ValueError(
                    f"scope_keys[{i}] must be non-empty and not contain '|' or '=', "
                    f"got {sk!r}"
                )
```

**spend()** -- add metadata:

```python
def spend(
    self,
    amount_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    metadata: dict[str, str] | None = None,  # NEW
) -> bool:
```

`metadata` values are `str` only. Non-str values raise `TypeError` at
`_build_scope_key()`. This is a deliberate constraint: scope keys are
string-keyed lookups, and coercing int/None silently would create
confusing key collisions.

### Internal changes

**BudgetEnforcer** -- nested buckets:

```python
# self._scoped_buckets: dict[str, dict[str, _PeriodBucket]] = {}
# Key structure: _scoped_buckets[scope_key][period_key] -> _PeriodBucket
```

`_build_scope_key()` builds a composite key from metadata. The key is
build-only, never parsed back:

```python
def _build_scope_key(self, metadata: dict[str, str] | None) -> str:
    if not self.scope_keys:
        return ""
    if metadata is None:
        return ""
    parts = []
    for key in self.scope_keys:
        val = metadata.get(key, "")
        parts.append(f"{key}={val.replace('|', '\\|')}")
    return "|".join(parts)
```

Note: `=` in values is NOT escaped. This is safe because keys are
build-only -- we never split on `=` to parse them back. The key is
used solely as a dict lookup key, so uniqueness is guaranteed by the
deterministic build order from `scope_keys`.

**Common bucket helper** -- `spend()` and `record_raw()` share the same
internal path for bucket lifecycle (tombstone re-admission, LRU eviction):

```python
def _get_or_create_bucket(self, scope_key: str, period_key: str) -> _PeriodBucket:
    """Get or create bucket, with tombstone re-admission and LRU eviction.

    MUST be called under self._lock.
    Used by both spend() and record_raw() to ensure identical behavior.
    """
    if scope_key not in self._scoped_buckets:
        # Re-admit from tombstone if available
        bucket = _PeriodBucket()
        mark = self._tombstones.pop(scope_key, None)
        if mark is not None:
            prior = mark.period_snapshots.get(period_key)
            if prior:
                bucket.cost, bucket.input_tokens, bucket.output_tokens = prior
        self._scoped_buckets[scope_key] = {period_key: bucket}
        # LRU eviction if over capacity
        if len(self._scoped_buckets) > self.max_scopes:
            self._evict_oldest()
    else:
        # Move to end for LRU freshness
        self._scoped_buckets.move_to_end(scope_key)

    return self._scoped_buckets[scope_key].setdefault(period_key, _PeriodBucket())
```

This ensures `record_raw()` (adapter path) and `spend()` (direct path)
have identical tombstone restoration and eviction behavior.

### What breaks

Nothing. `scope_keys=()` (default) uses a single `""` scope.

### Interaction with existing model

The 1:1 `BudgetTracker:ExecutionContext` model is preserved unchanged.
Composite scoping is BudgetEnforcer-only for long-lived services.

For multi-scope enforcement, users create a single `BudgetEnforcer`
with `scope_keys` and share it across requests. Each request also has
its own `ExecutionContext` with a flat `BudgetTracker` for per-request
hard limits.

**Coordinated spend protocol:** When both `ExecutionContext` and
`BudgetEnforcer` are active for the same request, the adapter MUST
evaluate them in dependency order:

1. **Cross-request enforcer first** (`BudgetEnforcer.spend()`).
   If denied, short-circuit -- do NOT record to per-request tracker.
   The call will not proceed, so per-request cost is zero.
2. **Per-request tracker second** (`ExecutionContext.add_cost_returning()`).
   Only recorded if the enforcer allowed.

This means an allowed spend is recorded by both systems (parallel
recording, not double-counting). A denied spend is recorded by
neither. The adapter's `spent_usd` returns the enforcer's cross-request
aggregate, not the per-request spend.

`record_budget_spend()` in `_shared.py` gains an optional `enforcer`
parameter to implement this coordination:

```python
def record_budget_spend(
    container, cost, tag, logger,
    input_tokens=0, output_tokens=0,
    enforcer: BudgetEnforcer | None = None,  # NEW
) -> bool:
    """Coordinated spend: enforcer first, then per-request tracker.

    Returns True if both allow, False if either denies.
    """
    if enforcer is not None:
        if not enforcer.spend(cost, input_tokens, output_tokens):
            return False  # short-circuit, tracker not updated

    # Per-request tracker (existing path)
    if container.budget is not None:
        container.budget.spend(cost)
    return True
```

### Edge cases

- **Memory growth**: LRU eviction of full buckets at `max_scopes`
  (mandatory). Use `collections.OrderedDict` with move-to-end on access.
- **Eviction preserves spend via tombstones**: When a scope is evicted
  from the full-bucket LRU, its per-period spend totals are collapsed
  into a compact `_HighWaterMark` tombstone. When the scope is re-admitted
  (new `spend()` arrives), the bucket is seeded from the tombstone so
  counters resume from prior values -- not zero.

  ```python
  @dataclass
  class _HighWaterMark:
      """Compact record kept after full-bucket eviction."""
      period_snapshots: dict[str, tuple[float, int, int]]
      # period_key -> (cost, input_tokens, output_tokens) at eviction time
  ```

  Tombstones are stored in a separate `_tombstones: dict[str, _HighWaterMark]`
  capped at `max_tombstones: int = 100_000`. If tombstones overflow, the
  oldest tombstone is evicted (counter-reset behavior, same as current LRU).
  This is the escape hatch for truly unbounded scope cardinality.

  **Known limitation**: tombstone eviction resets counters. For unbounded
  scope populations, use a persistent `budget_backend` (Redis/Postgres).
  In-memory tombstones are suitable for scope counts up to ~100K.
- **Key uniqueness**: Build-only keys, never parsed back. No collision
  possible given deterministic build from `scope_keys` tuple order.
- **Missing metadata fields**: Use empty string for missing keys.
- **AgentStepGuard**: Remains per-context, no changes.

---

## Adapter Feasibility: agent-control BudgetStore

### Target protocol

```python
class BudgetStore(Protocol):
    def record(
        self,
        scope_key: str,
        period_key: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> BudgetSnapshot: ...

@dataclass(frozen=True)
class BudgetSnapshot:
    spent_usd: float
    spent_tokens: int
```

### The pass-through problem

agent-control passes pre-built `scope_key` and `period_key` strings.
BudgetEnforcer builds these internally from config. Option (a) -- passing
agent-control's keys as metadata -- produces double-wrapped keys like
`"_scope=channel=slack-bot|user_id=alice"` and silently ignores
`period_key` (BudgetEnforcer derives its own period from the clock).

**Decision: option (b) -- add `record_raw()` to BudgetEnforcer.**

```python
def record_raw(
    self,
    scope_key: str,
    period_key: str,
    amount_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> _PeriodBucket:
    """Record spend with pre-built scope and period keys.

    Bypasses _build_scope_key() and _derive_period_key().
    Used by adapters that receive pre-built keys from external callers.
    Returns the updated bucket (snapshot of current totals for this scope+period).
    Thread-safe: acquires _lock for the entire read-modify-return.
    """
    with self._lock:
        bucket = self._get_or_create_bucket(scope_key, period_key)
        bucket.cost += amount_usd
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        # Return a copy so caller reads are not racy
        return _PeriodBucket(
            cost=bucket.cost,
            input_tokens=bucket.input_tokens,
            output_tokens=bucket.output_tokens,
        )
```

### Adapter sketch

```python
# veronica_core/adapters/agent_control.py

class VeronicaBudgetStore:
    """Adapts BudgetEnforcer to agent-control BudgetStore protocol."""

    def __init__(self, enforcer: BudgetEnforcer) -> None:
        self._enforcer = enforcer

    def record(
        self,
        scope_key: str,
        period_key: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> BudgetSnapshot:
        bucket = self._enforcer.record_raw(
            scope_key=scope_key,
            period_key=period_key,
            amount_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return BudgetSnapshot(
            spent_usd=bucket.cost,
            spent_tokens=bucket.input_tokens + bucket.output_tokens,
        )
```

### Gap analysis

| agent-control needs | veronica-core after enhancements | Gap? |
|---------------------|----------------------------------|------|
| scope_key grouping | `record_raw(scope_key, ...)` | No |
| period_key grouping | `record_raw(..., period_key, ...)` | No |
| Token tracking | `input_tokens` + `output_tokens` in spend/record_raw | No |
| USD tracking | `amount_usd` in spend/record_raw | No |
| Raw counters | `record_raw()` returns `_PeriodBucket` with `.cost/.input_tokens/.output_tokens` | No |
| No limit in store | BudgetStore caller owns limit checks | No |

No remaining gaps.

---

## Migration & Deprecation

No deprecation needed. All changes are additive:

| API | Before | After | Breaking? |
|-----|--------|-------|-----------|
| `ExecutionConfig.max_tokens` | N/A | `int \| None = None` (after existing defaults) | No |
| `BudgetEnforcer` fields | `limit_usd` | `+ limit_tokens, window, scope_keys, max_scopes, max_tombstones, clock` (all defaulted) | No |
| `BudgetEnforcer.spend()` | `(amount_usd)` | `(amount_usd, input_tokens=0, output_tokens=0, metadata=None)` | No |
| `BudgetEnforcer.record_raw()` | N/A | new method | No |
| `ContextSnapshot.tokens_accumulated` | N/A | `int = 0` | No |
| `record_budget_spend()` | `(container, cost, tag, logger)` | `+ input_tokens=0, output_tokens=0, enforcer=None` | No |

Note: `budget_window` is NOT on `ExecutionConfig`. It lives only on
`BudgetEnforcer`.

---

## Implementation Order

1. **Enhancement 1** (token limits) -- self-contained
2. **Enhancement 2** (time windows) -- independent of E1 (can use cost-only buckets), but E1-first is cleaner
3. **Enhancement 3** (composite scopes) -- adds scope dimension to E2's buckets
4. **Adapter** -- depends on all three + `record_raw()`

Each enhancement is independently shippable as a minor version bump.

---

## Test Strategy

Each enhancement ships with tests. Minimum coverage per enhancement:

**Enhancement 1 (token limits):**
- Boundary triple: `max_tokens - 1` (allow), `max_tokens` (deny), `max_tokens + 1` (deny)
- Zero token limit: `max_tokens=0` denies first call
- Negative/non-integer token validation: `ValueError` / `TypeError`
- `spend()` backward compat: existing `spend(amount_usd)` calls still work
- `spent_tokens` / `remaining_tokens` properties under concurrent access
- `check_limits()` emits `"token_budget_exceeded"` with correct emit_fn call

**Enhancement 2 (time windows):**
- Period rollover: inject clock, advance past midnight, verify new bucket
- Clock injection: mock clock returns deterministic timestamps
- ISO week year edge case: Dec 31 in ISO week 1 of next year
- `window=None` produces single `""` bucket (backward compat)
- Invalid window value rejected in `__post_init__`
- Concurrent spend at period boundary: two threads, one per period

**Enhancement 3 (composite scopes):**
- Tombstone eviction + re-admission: evict scope, re-spend, verify counters
  resume from prior values (not zero)
- Tombstone overflow: exceed `max_tombstones`, verify oldest tombstone evicted
- LRU eviction order: spend on 3 scopes, verify least-recently-used evicted
- `scope_keys` validation: empty string, `|` in key, `=` in key all rejected
- `_build_scope_key()` determinism: same metadata produces same key
- `record_raw()` uses same `_get_or_create_bucket()` as `spend()`

**Adapter:**
- `record_raw()` -> `BudgetSnapshot` round-trip: record, read bucket, verify totals
- `VeronicaBudgetStore` implements `BudgetStore` protocol (structural check)

**Coordination:**
- Enforcer deny -> tracker NOT updated (short-circuit)
- Enforcer allow + tracker deny -> both recorded, `allowed=False`
- Neither active -> `allowed=True` (no-op path)

---

## Thread Safety

**BudgetTracker** (per-request, inside ExecutionContext):
- Single `_lock` protects `_cost`, `_input_tokens`, `_output_tokens`
- No dict structures, no new complexity
- Same threading model as current

**BudgetEnforcer** (long-lived, shared across requests):
- Single `_lock` protects `_scoped_buckets` dict, `_tombstones` dict,
  and all bucket mutations
- `_derive_period_key()` and `_build_scope_key()` run OUTSIDE `_lock`
  (clock() is never called under lock -- see Enhancement 2 spend() sketch)
- Bucket lookup + mutation is atomic under `_lock`
- LRU eviction + tombstone creation runs under `_lock`

**Universal rule: clock() is never called under any lock.** This applies
to `spend()`, `record_raw()`, and any future method that derives period
keys. `_derive_period_key()` and `_build_scope_key()` are pure functions
of their inputs and run before lock acquisition. The TOCTOU window
between key derivation and lock acquisition is bounded by thread
scheduling jitter (microseconds) and is acceptable for daily/weekly/monthly
windows.

**Two independent locks:** BudgetEnforcer and BudgetTracker each own
their own `self._lock`. These are independent objects serving independent
use cases (cross-request vs per-request). The coordinated spend protocol
(see Enhancement 3) evaluates the enforcer first, then the tracker. Each
acquires only its own lock -- the two locks are never held simultaneously
by the same thread. No nested-lock deadlock risk.
