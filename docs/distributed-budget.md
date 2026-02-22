# Distributed Budget Backend

When multiple processes run VERONICA chains concurrently (e.g. parallel API workers), each
process tracks its own in-memory cost accumulator. There's no shared view across processes,
so a global per-chain budget ceiling can't be enforced.

`veronica_core.distributed` solves this with two backends:

- **`LocalBudgetBackend`**: in-process, thread-safe accumulator (the default).
- **`RedisBudgetBackend`**: cross-process accumulator backed by Redis `INCRBYFLOAT`.

## Usage

### Default (no Redis)

Nothing to configure. `ExecutionContext` uses `LocalBudgetBackend` automatically.

### With Redis

Pass `redis_url` to `ExecutionConfig` and the backend wires up automatically:

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

config = ExecutionConfig(
    max_cost_usd=1.0,
    max_steps=50,
    max_retries_total=10,
    redis_url="redis://localhost:6379",
)
with ExecutionContext(config=config) as ctx:
    ctx.wrap_llm_call(fn=my_llm_call)
```

Or pass an explicit backend if you need more control:

```python
from veronica_core.distributed import RedisBudgetBackend
from veronica_core.containment import ExecutionConfig, ExecutionContext

backend = RedisBudgetBackend(
    redis_url="redis://localhost:6379",
    chain_id="my-chain-id",
    ttl_seconds=3600,
    fallback_on_error=True,
)
config = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10,
                         budget_backend=backend)
```

## When Redis is unavailable

`RedisBudgetBackend` won't crash your application:

- **On connection failure** (`fallback_on_error=True`, the default): logs a warning and
  falls back to `LocalBudgetBackend` automatically for all subsequent operations.
- **On mid-run failure**: same — logs an error, switches to local state.
- **`is_using_fallback` property**: check whether a fallback is active.

Set `fallback_on_error=False` if you'd rather have exceptions raised.

## Key format

```
veronica:budget:{chain_id}
```

The `chain_id` comes from `ChainMetadata`. Processes sharing the same `chain_id` accumulate
cost into the same key.

## TTL

Keys expire via `EXPIRE` after each `INCRBYFLOAT`. The default is **3600 seconds**. The TTL
resets on every write, so active chains don't expire mid-run.

```python
RedisBudgetBackend(redis_url=..., chain_id=..., ttl_seconds=7200)
# or via the factory:
get_default_backend(redis_url=..., chain_id=..., ttl_seconds=7200)
```

## Thread safety

Both backends are thread-safe. `RedisBudgetBackend` batches `INCRBYFLOAT` and `EXPIRE` into
a single pipeline round-trip.

## Install

```bash
pip install veronica-core[redis]
# or with uv:
uv add veronica-core[redis]
```

## Testing with fakeredis

See `tests/test_distributed.py` for full examples using `fakeredis.FakeRedis` as a drop-in
replacement for a real Redis connection.
