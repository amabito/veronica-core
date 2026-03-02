# Distributed Circuit Breaker — Deployment Guide

This document covers how to deploy `DistributedCircuitBreaker` in a multi-process
environment, configure Redis, and understand how local and Redis state are
kept in sync.

---

## Overview

`veronica_core.distributed` provides two classes for cross-process coordination:

| Class | Backing store | Use case |
|---|---|---|
| `LocalBudgetBackend` | In-process `threading.Lock` | Single-process chains |
| `RedisBudgetBackend` | Redis `INCRBYFLOAT` | Cross-process cost accumulation |
| `DistributedCircuitBreaker` | Redis hash + Lua scripts | Cross-process failure isolation |

The local `CircuitBreaker` (in `veronica_core.circuit_breaker`) tracks state
per-process. `DistributedCircuitBreaker` wraps the same public API but stores
`CLOSED/OPEN/HALF_OPEN` state in a Redis hash, so every process reading the
same `circuit_id` key sees the same circuit state.

---

## Prerequisites

```bash
# Install redis extra
pip install veronica-core[redis]
# or with uv
uv add veronica-core[redis]
```

Redis >= 5.0 is required for the Lua scripting features used by the atomic
state transitions.

---

## Basic Usage

```python
from veronica_core.distributed import DistributedCircuitBreaker
from veronica_core.runtime_policy import PolicyContext

breaker = DistributedCircuitBreaker(
    redis_url="redis://localhost:6379",
    circuit_id="my-llm-service",
    failure_threshold=5,
    recovery_timeout=60.0,
)

ctx = PolicyContext()
decision = breaker.check(ctx)
if decision.allowed:
    try:
        result = call_llm()
        breaker.record_success()
    except Exception:
        breaker.record_failure()
else:
    # Circuit is OPEN — skip the call
    handle_degraded_response()
```

All state reads and writes go through atomic Lua scripts, so no distributed lock
is needed at the application level.

---

## Configuration Options

| Parameter | Type | Default | Description |
|---|---|---|---|
| `redis_url` | `str` | required | Redis connection URL |
| `circuit_id` | `str` | required | Redis key suffix; all processes sharing this value coordinate on the same circuit |
| `failure_threshold` | `int` | `5` | Consecutive failures before the circuit opens |
| `recovery_timeout` | `float` | `60.0` | Seconds in OPEN state before transitioning to HALF_OPEN |
| `ttl_seconds` | `int` | `3600` | Redis key TTL; refreshed on every write |
| `fallback_on_error` | `bool` | `True` | Fall back to a local `CircuitBreaker` if Redis is unreachable |
| `half_open_slot_timeout` | `float` | `120.0` | Seconds before an unclaimed HALF_OPEN test slot is auto-released; set to `0` to disable |
| `redis_client` | `redis.Redis` | `None` | Pass an existing client to share a connection pool |

---

## Redis Key Format

```
veronica:circuit:{circuit_id}
```

The key is a Redis hash with the following fields:

| Field | Type | Description |
|---|---|---|
| `state` | string | `CLOSED`, `OPEN`, or `HALF_OPEN` |
| `failure_count` | int | Current consecutive failure count |
| `success_count` | int | Lifetime success count |
| `last_failure_time` | float | Unix timestamp of the most recent failure |
| `half_open_in_flight` | int | `1` when one process holds the HALF_OPEN test slot |
| `half_open_claimed_at` | float | Unix timestamp when the slot was claimed (for timeout) |

---

## Delta Reconciliation — How Local and Redis State Sync

When Redis becomes unreachable, `DistributedCircuitBreaker` falls back to a
local `CircuitBreaker`. The local breaker accumulates failure/success history
while Redis is down. When Redis comes back:

1. The local circuit state (`CLOSED/OPEN/HALF_OPEN`, `failure_count`, etc.) is
   pushed to Redis via a pipelined `HSET`.
2. The other processes read the reconciled state on their next `check()` call.
3. The local fallback is cleared.

This is called *delta reconciliation*. It works in one direction: local
accumulated state is pushed to Redis. Any state written by other processes
during the outage is overwritten by the reconciling process.

**Trade-off**: If multiple processes were isolated during the same Redis outage,
the last one to reconnect wins. In practice this means the circuit may close
slightly earlier than it would have if Redis had been available throughout.
The system fails toward availability, not strictness.

### RedisBudgetBackend delta reconciliation

`RedisBudgetBackend` uses a different strategy for cost accumulation. During
fallback, the local backend tracks spend independently. On reconnection, only
the *delta* (spend accumulated since failover) is flushed to Redis with
`INCRBYFLOAT`. The formula is:

```
delta = local_fallback.get() - fallback_seed_base
```

`fallback_seed_base` is the Redis total at the moment of failover, captured
before the transition. This ensures the base already present in Redis is not
counted twice.

---

## HALF_OPEN Slot Semantics

When the circuit transitions from OPEN to HALF_OPEN (after `recovery_timeout`
elapses), exactly one process is allowed to send a test call. The Lua `check`
script claims the slot atomically by setting `half_open_in_flight = 1` only if
the current value is `0`.

If the claiming process crashes without calling `record_success()` or
`record_failure()`, the slot would stay claimed indefinitely. To prevent
permanent lock-out, configure `half_open_slot_timeout`:

```python
breaker = DistributedCircuitBreaker(
    redis_url="redis://localhost:6379",
    circuit_id="my-service",
    half_open_slot_timeout=30.0,  # release after 30 seconds if not resolved
)
```

**Recommended value**: `2 * max_llm_call_timeout`. If your LLM calls time out
after 10 seconds, set `half_open_slot_timeout=20.0`.

Setting `half_open_slot_timeout=0` disables the auto-release. The slot then
persists until the key TTL expires or `reset()` is called.

---

## Connection Pool Sharing

When multiple `DistributedCircuitBreaker` instances protect different services
in the same process, pass a shared `redis.Redis` client to avoid opening
redundant connections:

```python
import redis as redis_lib
from veronica_core.distributed import DistributedCircuitBreaker

pool = redis_lib.ConnectionPool.from_url("redis://localhost:6379", max_connections=10)
shared_client = redis_lib.Redis(connection_pool=pool, decode_responses=True)

breaker_llm = DistributedCircuitBreaker(
    redis_url="redis://localhost:6379",
    circuit_id="llm-service",
    redis_client=shared_client,
)
breaker_search = DistributedCircuitBreaker(
    redis_url="redis://localhost:6379",
    circuit_id="search-service",
    redis_client=shared_client,
)
```

---

## AG2 Multi-Agent Example

`CircuitBreakerCapability` integrates with AG2's `ConversableAgent` pattern.
For distributed coordination, pass a `DistributedCircuitBreaker` via the
`failure_predicate` parameter or use it directly alongside the capability.

The pattern below shows three agents sharing a single distributed circuit:

```python
from veronica_core.distributed import DistributedCircuitBreaker
from veronica_core.runtime_policy import PolicyContext

# Each worker process creates its own breaker pointing at the same circuit_id.
# Redis is the coordination point.
breaker = DistributedCircuitBreaker(
    redis_url="redis://your-redis:6379",
    circuit_id="planner-llm",
    failure_threshold=3,
    recovery_timeout=30.0,
)

def safe_llm_call(agent_name: str, messages: list) -> str | None:
    ctx = PolicyContext()
    decision = breaker.check(ctx)
    if not decision.allowed:
        print(f"[{agent_name}] circuit {decision.reason}")
        return None
    try:
        result = call_llm(messages)
        breaker.record_success()
        return result
    except Exception as exc:
        breaker.record_failure()
        raise
```

See `examples/distributed_circuit_breaker_demo.py` for a runnable version
using `fakeredis` (no real Redis required).

---

## Production Checklist

### Redis persistence

Enable AOF persistence so circuit state survives Redis restarts:

```conf
# redis.conf
appendonly yes
appendfsync everysec
```

Without persistence, a Redis restart resets all circuits to `CLOSED`. This is
usually acceptable — the circuit re-opens quickly if the underlying service is
still unhealthy.

### Connection pooling

Use `ConnectionPool` with a bounded `max_connections` to avoid connection storms
during traffic spikes. A pool of 10–20 connections is sufficient for most
deployments.

### Monitoring

Key metrics to track:

| Metric | How to observe |
|---|---|
| Circuit state | `breaker.state` or `breaker.snapshot().state` |
| Failure count | `breaker.snapshot().failure_count` |
| Using fallback | `breaker.is_using_fallback` |
| Redis memory | `redis-cli INFO memory` |

Log lines emitted by the library use the prefix `[VERONICA_CIRCUIT]` so they
can be filtered independently.

### Failover (Redis Sentinel / Cluster)

Pass the Sentinel URL to `redis_url`:

```python
breaker = DistributedCircuitBreaker(
    redis_url="redis+sentinel://sentinel1:26379,sentinel2:26379/mymaster",
    circuit_id="my-service",
)
```

For Redis Cluster, use `rediscluster` or `redis-py-cluster` and pass the
client directly via `redis_client`.

If Redis goes down entirely and `fallback_on_error=True` (the default), the
breaker automatically switches to a local `CircuitBreaker`. Circuit opens and
closures during the outage are reconciled when Redis comes back.

### TTL sizing

The default `ttl_seconds=3600` means idle circuit keys expire after one hour
with no activity. Size this to be longer than your longest expected quiet
period. For services that run 24/7, `ttl_seconds=86400` (24 hours) is a
conservative choice.

---

## Testing Without Redis

Use `fakeredis` in tests and demos:

```python
import fakeredis
import threading
from veronica_core.circuit_breaker import CircuitBreaker
from veronica_core.distributed import DistributedCircuitBreaker
import veronica_core.distributed as _dist

def make_test_breaker(fake_client, circuit_id="test"):
    dcb = DistributedCircuitBreaker.__new__(DistributedCircuitBreaker)
    dcb._redis_url = "redis://fake"
    dcb._circuit_id = circuit_id
    dcb._key = f"veronica:circuit:{circuit_id}"
    dcb._failure_threshold = 3
    dcb._recovery_timeout = 60.0
    dcb._ttl = 3600
    dcb._fallback_on_error = True
    dcb._half_open_slot_timeout = 120.0
    dcb._fallback = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
    dcb._using_fallback = False
    dcb._client = fake_client
    dcb._owns_client = False
    dcb._lock = threading.Lock()
    dcb._last_reconnect_attempt = 0.0
    dcb._script_failure = fake_client.register_script(_dist._LUA_RECORD_FAILURE)
    dcb._script_success = fake_client.register_script(_dist._LUA_RECORD_SUCCESS)
    dcb._script_check = fake_client.register_script(_dist._LUA_CHECK)
    return dcb

server = fakeredis.FakeServer()
client = fakeredis.FakeRedis(server=server, decode_responses=True)
breaker = make_test_breaker(client)
```

See `tests/test_distributed_circuit_breaker.py` for complete test patterns
including concurrency, corrupted state, and Redis-down scenarios.
