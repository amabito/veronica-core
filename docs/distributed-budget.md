# Distributed Budget Backend

## Problem

When multiple processes run VERONICA chains concurrently (e.g., multiple API workers),
each process tracks its own `cost_usd_accumulated` in-memory. There is no shared view
of the total cost across processes, making it impossible to enforce a global chain-level
budget ceiling.

## Solution

`veronica_core.distributed` provides a `BudgetBackend` protocol with two implementations:

- **`LocalBudgetBackend`**: In-process, thread-safe accumulator (default).
- **`RedisBudgetBackend`**: Cross-process accumulator backed by Redis `INCRBYFLOAT`.

## Usage

### Default (no Redis)

No changes required. `ExecutionContext` uses `LocalBudgetBackend` automatically.

### With Redis URL (auto-creation)

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

config = ExecutionConfig(
    max_cost_usd=1.0,
    max_steps=50,
    max_retries_total=10,
    redis_url="redis://localhost:6379",  # auto-creates RedisBudgetBackend
)
with ExecutionContext(config=config) as ctx:
    ctx.wrap_llm_call(fn=my_llm_call)
```

### With explicit backend

```python
from veronica_core.distributed import RedisBudgetBackend
from veronica_core.containment import ExecutionConfig, ExecutionContext

backend = RedisBudgetBackend(
    redis_url="redis://localhost:6379",
    chain_id="my-chain-id",
    ttl_seconds=3600,
    fallback_on_error=True,
)
config = ExecutionConfig(
    max_cost_usd=1.0,
    max_steps=50,
    max_retries_total=10,
    budget_backend=backend,
)
```

## Failsafe Behavior

`RedisBudgetBackend` is designed to never crash your application:

- **On connection failure** (`fallback_on_error=True`, default): logs a warning and switches
  to `LocalBudgetBackend` automatically. All subsequent operations use local state.
- **On operation failure** (Redis down mid-run): logs an error, switches to local fallback.
- **`is_using_fallback` property**: inspect whether the backend has fallen back.

Set `fallback_on_error=False` to raise exceptions instead (useful for strict environments).

## Key Naming

Redis keys follow the pattern:

```
veronica:budget:{chain_id}
```

Each `ExecutionContext` uses its `chain_id` (from `ChainMetadata`) as the suffix.
Multiple processes sharing the same `chain_id` will accumulate cost into the same key.

## TTL Configuration

Keys expire automatically via `EXPIRE` set after each `INCRBYFLOAT`. Default TTL is
**3600 seconds (1 hour)**. Configure via:

```python
RedisBudgetBackend(redis_url=..., chain_id=..., ttl_seconds=7200)
# or via get_default_backend factory:
get_default_backend(redis_url=..., chain_id=..., ttl_seconds=7200)
```

The TTL is reset on every `add()` call, so long-running chains do not expire mid-run
as long as they are active.

## Thread Safety

Both backends are thread-safe:

- `LocalBudgetBackend`: uses `threading.Lock`.
- `RedisBudgetBackend`: Redis `INCRBYFLOAT` is atomic; pipeline batches `INCRBYFLOAT`
  and `EXPIRE` into a single round-trip.

## Installing Redis Extra

```bash
pip install veronica-core[redis]
# or with uv:
uv add veronica-core[redis]
```

## Testing with fakeredis

```python
import fakeredis
from veronica_core.distributed import RedisBudgetBackend, LocalBudgetBackend
import threading

server = fakeredis.FakeServer()
client = fakeredis.FakeRedis(server=server, decode_responses=True)

# Inject fake client directly (bypasses _connect)
backend = RedisBudgetBackend.__new__(RedisBudgetBackend)
backend._client = client
backend._key = "veronica:budget:test"
backend._ttl = 3600
backend._fallback_on_error = True
backend._fallback = LocalBudgetBackend()
backend._using_fallback = False
backend._lock = threading.Lock()
```
