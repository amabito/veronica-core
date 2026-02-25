# VERONICA Core + AG2

AG2 (AutoGen) retries failed agents indefinitely — VERONICA adds a
per-agent circuit breaker and a system-wide SAFE_MODE to stop runaway
loops before they drain your API budget.

## How it works

- **Circuit breaker** — each agent has its own fail counter; after
  `cooldown_fails` consecutive `None` replies the agent is frozen for
  `cooldown_seconds` seconds.
- **SAFE_MODE** — an orchestrator (or any monitoring code) can call
  `veronica.state.transition(VeronicaState.SAFE_MODE, reason="...")` to
  block all agents instantly, regardless of their individual counters.

## Quick start

```python
from veronica_core import VeronicaIntegration
from veronica_core.backends import MemoryBackend
from veronica_core.state import VeronicaState

veronica = VeronicaIntegration(
    cooldown_fails=3,
    cooldown_seconds=60,
    backend=MemoryBackend(),
)

def guarded_reply(agent, messages):
    if veronica.state.current_state == VeronicaState.SAFE_MODE:
        return None  # system halt

    if veronica.is_in_cooldown(agent.name):
        return None  # this agent is cooling down

    reply = agent.generate_reply(messages)

    if reply is None:
        veronica.record_fail(agent.name)
    else:
        veronica.record_pass(agent.name)

    return reply
```

Replace your existing `agent.generate_reply(messages)` calls with
`guarded_reply(agent, messages)`.

## Run the demo

```bash
uv run python examples/integrations/autogen/example.py
```

No API key required — the demo uses stub agents.

## Demos included

| Demo | What it shows |
|------|---------------|
| `demo_circuit_breaker` | Fail counter rises, cooldown activates, agent is skipped |
| `demo_safe_mode` | Orchestrator triggers system-wide halt, all agents blocked |
| `demo_per_agent_tracking` | Healthy agent keeps running while broken agent cools down |

## Persist cooldown state across restarts

Swap `MemoryBackend` for `JSONBackend` to survive process restarts:

```python
from veronica_core.backends import JSONBackend

veronica = VeronicaIntegration(
    cooldown_fails=3,
    cooldown_seconds=60,
    backend=JSONBackend(path="veronica_state.json"),
)
```

## SAFE_MODE

Trigger a system-wide halt from anywhere in your orchestration code:

```python
veronica.state.transition(
    VeronicaState.SAFE_MODE,
    reason="Cost spike detected — manual review required",
)
```

Clear it once the issue is resolved:

```python
veronica.state.transition(VeronicaState.SCREENING, reason="Manual review passed")
```

While SAFE_MODE is active, every `guarded_reply` call returns `None`
without invoking the agent.

## Requirements

```
pip install veronica-core
pip install ag2          # optional — not required for the demo
```
