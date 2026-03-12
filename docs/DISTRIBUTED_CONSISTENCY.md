# Distributed Consistency Model

veronica-core enforces budget ceilings across concurrent LLM calls using a two-phase
reserve/commit/rollback protocol. This document describes the protocol, the atomicity
guarantees provided by each backend, failure recovery paths, and the overall consistency
model.

---

## Budget Transaction Protocol

Each `wrap_llm_call` or `wrap_tool_call` invocation that carries a `cost_estimate_hint`
follows a two-phase accounting flow in `ExecutionContext._wrap`
(`src/veronica_core/containment/execution_context.py`, line ~605):

```
1. reserve(amount, ceiling)  -- escrow the estimated cost
2. fn()                      -- execute the LLM call
3. commit(reservation_id)    -- move escrow to committed total  (success path)
   OR rollback(reservation_id)  -- release escrow without charging  (error path)
```

**Reserve (escrow)**: Before dispatching `fn`, the backend checks whether
`committed + active_reservations + amount <= ceiling`. If the check passes, the amount
is held in escrow (a UUID reservation ID is returned). If not, `OverflowError` is raised
and `_wrap` returns `Decision.HALT` without calling `fn`.

**Commit**: On the success path, `_finalize_success` calls `commit(reservation_id)`.
The escrow entry is deleted and the amount is added to the committed total atomically.
The new committed total is returned.

**Rollback**: On any failure path -- pipeline pre-check rejection, circuit-breaker denial,
`fn` raising an exception, or an unexpected `BaseException` in the outer `except` clause --
`_try_rollback` is called. It swallows all exceptions so no secondary failure can mask
the original error.

The `_reservation_id` variable is initialised to `None` before the `try` block. This
means `_try_rollback` is always safe to call regardless of whether `reserve` was reached.

---

## LocalBudgetBackend

`LocalBudgetBackend` (`src/veronica_core/distributed.py`, line 73) provides in-process,
thread-safe two-phase accounting using `threading.Lock`.

**Data layout**:
```
_cost: float                           -- committed total
_reservations: dict[rid, (amount, deadline_monotonic)]
_reserved_total: float                 -- running sum (reset to 0 when dict empty)
```

**Reserve atomicity**: `reserve()` acquires `_lock`, sweeps expired entries, then checks
`_cost + reserved_total + amount <= ceiling + 1e-9`. The epsilon compensates for IEEE-754
rounding when accumulating small floats. If the check passes, the reservation is inserted
and `_reserved_total` is incremented under the same lock acquisition.

**Float drift prevention**: `_total_reserved_locked` resets `_reserved_total` to `0.0`
whenever `_reservations` is empty. This prevents accumulation of rounding errors that
would otherwise grow over a long-running process.

**Expiry**: Reservations expire after `_RESERVATION_TIMEOUT_S` (60 seconds). Expired
entries are swept lazily in `_expire_reservations_locked`, which is called at the start of
`reserve`, `commit`, and `rollback`.

---

## RedisBudgetBackend

`RedisBudgetBackend` (`src/veronica_core/distributed.py`, line 190) provides cross-process
budget coordination. All mutations are performed inside Lua scripts executed on the Redis
server, so each operation is a single round-trip with server-side atomicity.

### Redis key layout

```
veronica:budget:{chain_id}               -- INCRBYFLOAT committed total (string/float)
veronica:budget:{chain_id}:reservations  -- HASH: rid -> "amount:deadline_unix"
```

### Lua scripts

**Reserve** (`lua_reserve`, line ~496): Single-pass sweep using `HGETALL` on the
reservations hash. For each entry, the Lua script parses `amount:deadline`, deletes
expired entries via `HDEL`, and sums active reservations. It then fetches the committed
total with `GET` and checks `committed + reserved_total + amount > ceiling + 1e-9`. On
overflow, it returns a Redis error reply. On success, it `HSET`s the new reservation.
The entire sweep-and-check-and-insert sequence runs in one Redis call.

**Commit** (`lua_commit`, line ~576): Fetches the reservation with `HGET`, deletes it
with `HDEL`, then calls `INCRBYFLOAT` to add the amount to the committed key. Both the
deletion and the increment happen in the same Lua execution context -- no other client can
observe an intermediate state where the reservation is gone but the committed total has
not yet been updated.

**Rollback** (`lua_rollback`, line ~640): Calls `HDEL` on the reservation. Returns an
error reply if the key did not exist. No committed total is modified.

### INCRBYFLOAT and epsilon

Redis `INCRBYFLOAT` accumulates IEEE-754 rounding errors across many small increments.
`_BUDGET_EPSILON = 1e-9` is applied to all ceiling comparisons to avoid spurious
under-enforcement (e.g. a stored value of `9.999999999999998` incorrectly blocking a
ceiling of `10.0`). This constant is defined at module level with a comment explaining
the rationale.

### TTL

After each `INCRBYFLOAT`, the committed key TTL is reset via `EXPIRE` inside
`lua_commit`. The default is 3600 seconds. Keys for completed chains expire automatically and do not accumulate indefinitely.

---

## Failure Recovery

### Network partition -- Redis unreachable

When `fallback_on_error=True` (the default), `RedisBudgetBackend` catches connection
errors in `reserve`, `commit`, and `rollback`, logs the error with the Redis URL redacted
(via `_redact_exc`), and switches to `LocalBudgetBackend` for the remainder of the
process lifetime.

Before switching, `_seed_fallback_from_redis` reads the current committed total from
Redis and seeds the local backend with that value. Subsequent local increments are tracked
as a delta above this seed base. On reconnection, `_reconcile_on_reconnect` flushes the
delta back to Redis using `INCRBYFLOAT(delta)` to avoid double-counting.

The `is_using_fallback` property exposes whether the fallback is currently active.

### Reservation timeout (60-second auto-expiry)

If a process crashes, hangs, or is killed between `reserve` and `commit`/`rollback`, the
reservation entry persists in Redis. The 60-second deadline stored in the reservation
value (`amount:deadline_unix`) causes the entry to be swept during the next `reserve`
call's Lua sweep pass, or discarded passively as the hash entry ages out. This prevents
indefinite budget lock-up from crashed callers.

### Exception in fn

Any exception raised by `fn` causes `_wrap` to call `_try_rollback` before re-raising or
returning `Decision.HALT`. The rollback is unconditional: if `fn` raises `KeyboardInterrupt`
or any `BaseException`, the outer `except BaseException` block in `_wrap` calls
`_try_rollback` before re-raising.

### Estimated vs actual cost drift

Because `lua_commit` executes `HDEL` and `INCRBYFLOAT` atomically in a single Lua script,
a partial commit (reservation deleted but total not incremented) cannot occur during normal
operation. However, the *estimated* cost reserved before `fn()` may differ from the *actual*
cost computed after `fn()` completes. Over many calls, these small differences accumulate.
The `ReconciliationCallback` protocol (`src/veronica_core/protocols.py`, line 342) provides
a hook for callers to detect and compensate for this drift:

```python
class ReconciliationCallback(Protocol):
    def on_reconcile(self, estimated_cost: float, actual_cost: float) -> None: ...
```

`on_reconcile` is called after every successful `_finalize_success` with the estimated
cost (the `cost_estimate_hint`) and the actual cost computed from the response. Persistent
divergence between estimated and actual indicates drift that the caller may want to
surface.

---

## Failure Categories and Handling

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Redis unreachable on connect | `ConnectionError` in `_connect` | Fallback to `LocalBudgetBackend` |
| Redis fails mid-operation | Exception in `reserve`/`commit`/`rollback` | Fallback after seeding from last known Redis total |
| Caller crashes between reserve and commit | 60-second reservation deadline | Auto-swept on next `reserve` Lua call |
| `fn` raises exception | `_fn_exc is not None` in `_wrap` | `_try_rollback`, then `_handle_fn_error` |
| Budget ceiling exceeded | `OverflowError` from `reserve` | `Decision.HALT` returned, no `fn` call |
| Unexpected `BaseException` | `except BaseException` in `_wrap` | `_try_rollback`, re-raise |

---

## Consistency Guarantees

**At-most-once commit**: `lua_commit` deletes the reservation entry with `HDEL` and
performs `INCRBYFLOAT` in the same Lua script. A reservation cannot be committed twice
because the second attempt finds an empty `HGET` result and returns an error reply.

**Ceiling enforcement is conservative**: Both backends add `_BUDGET_EPSILON = 1e-9`
to the ceiling during the check (`committed + reserved + amount <= ceiling + epsilon`).
The budget may be slightly over-committed in theory but will not be significantly
under-enforced due to float representation.

**Atomic budget updates**: All Redis mutations that change budget state (reserve,
commit, rollback) run in Lua scripts. There is no window in which the committed total
and the reservation hash are inconsistent from the perspective of another Redis client.

**Eventual consistency after failover**: During Redis fallback, cost accumulates locally.
After reconnection, `_reconcile_on_reconnect` flushes the delta to Redis. Between
failover and reconnection, a parallel process reading the Redis committed total will see
a lower value than the true total. This window is bounded by the reconnection interval
(`_RECONNECT_INTERVAL = 5.0` seconds).

---

## SharedTimeoutPool

`SharedTimeoutPool` (`src/veronica_core/containment/timeout_pool.py`) provides a module-level
singleton for timeout scheduling. A single daemon thread (`veronica-timeout-pool`) wakes
at the earliest scheduled deadline using a `heapq` priority queue. Cancellation is O(1)
via a `_cancelled` set checked at fire time.

This pool replaces per-context `threading.Thread` timeout threads. The daemon thread is
started lazily on the first `schedule()` call and shared across all `ExecutionContext`
instances in the process.

---

## ReconciliationCallback Protocol

`ReconciliationCallback` (`src/veronica_core/protocols.py`, line 342) is a
`@runtime_checkable` Protocol. Implement it to receive post-call cost reconciliation
events:

```python
from veronica_core.protocols import ReconciliationCallback

class MyCostTracker:
    def on_reconcile(self, estimated_cost: float, actual_cost: float) -> None:
        drift = actual_cost - estimated_cost
        if abs(drift) > 0.01:
            logger.warning("cost drift %.4f USD", drift)
```

Pass the instance to `WrapOptions.reconciliation_callback`. The callback is invoked
synchronously on the success path in `_finalize_success`. It must not raise.
