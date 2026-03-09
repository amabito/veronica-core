---
title: "The $0.64 bug: how nested retries silently multiply your LLM costs"
published: false
tags: python, llm, langchain, ai
---

One user click. One document. My LangChain agent made 64 API calls to GPT-4o before it finally returned a result.

At typical GPT-4o pricing, that turns a one-cent task into a sixty-cent task. With longer prompts, worse.

The agent wasn't broken. The bug was in how the retries *multiply*.

---

## The problem: retries stack, and nobody tracks the total

This pattern shows up in most LLM agent stacks I've looked at:

```
Your application code        retries 3 times on failure
  calls a LangChain chain    retries 3 times on failure
    which calls a tool        retries 3 times on failure
      which calls the LLM API
```

Each layer is reasonable on its own. 3 retries is a perfectly normal default.

When the LLM returns a transient error, the innermost layer retries 3 times. The middle layer sees a failure, retries -- triggering 3 more inner retries each time. Outer layer does the same.

Worst case: **4 x 4 x 4 = 64 API calls from a single user action.**

(Each layer makes 1 initial attempt + 3 retries = 4 attempts. Three layers: `4^3 = 64`.)

Nobody in the stack tracks the *total* retry count. Each layer only knows about its own attempts. I built [veronica-core](https://github.com/amabito/veronica-core) to fix this -- a run-level budget that sits across all layers.

## The math

| Retry layers | Retries per layer | Worst-case calls |
|--------------|-------------------|------------------|
| 2            | 3                 | 16               |
| 3            | 3                 | 64               |
| 4            | 3                 | 256              |
| 3            | 5                 | 216              |

This is exponential, not linear. Adding one more retry layer doesn't add 3 calls -- it multiplies the total by 4.

## What this costs

GPT-4o at $2.50/1M input + $10.00/1M output tokens. A typical 2K-token agent step with a 500-token response costs about $0.01.

| Scenario | Calls | Cost per request |
|----------|-------|------------------|
| No retries needed | 1 | $0.01 |
| 3-layer retry, worst case | 64 | $0.64 |
| 4-layer retry, worst case | 256 | $2.56 |
| 1000 users/day, 3-layer worst case | 64,000 | $640/day |

Most of the time you won't hit worst case. But you will hit partial amplification regularly -- 8-12 calls where 1-2 would suffice. That's a steady 4-6x cost multiplier that shows up as "the API is expensive" rather than "our retry logic is broken."

## Why `max_iterations` doesn't fix this

Most agent frameworks have some form of step or iteration limit. LangChain has `max_iterations`, others have conversation turn caps or loop counters. These limit how many *steps* your agent takes, not how many API calls happen underneath.

If an agent has `max_iterations=10` and each iteration retries 3 times internally, you can still get 40 API calls. The step counter doesn't see the retries.

These are step limits, not cost limits. None of them track how much money the run has spent.

## Before: no containment

This example is intentionally simplified. In real LangChain stacks, the same multiplication usually comes from a mix of provider retries, `tenacity` decorators, tool wrappers, and chain-level retries -- which makes it hard to spot by reading any single file.

```python
import time
import random

def call_llm(prompt: str) -> str:
    """Calls the LLM API. Fails sometimes."""
    if random.random() < 0.3:
        raise RuntimeError("API timeout")
    return "result"

def inner_tool(prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            return call_llm(prompt)
        except RuntimeError:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.1)

def chain_step(prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            return inner_tool(prompt)
        except RuntimeError:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.1)

def agent_run(prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            return chain_step(prompt)
        except RuntimeError:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.1)

# One user click. Up to 64 API calls. No total limit.
result = agent_run("Summarize this document")
```

Every layer is doing the right thing locally. But nobody tracks the total. No budget, no circuit breaker. If the API goes down for 30 seconds, this burns through 64 calls before giving up.

## After: chain-level containment

```python
from veronica_core.containment import ExecutionContext, ExecutionConfig
from veronica_core.shield.types import Decision

config = ExecutionConfig(
    max_cost_usd=0.10,       # Hard dollar ceiling for this run
    max_steps=20,            # Max successful operations
    max_retries_total=5,     # Total retries across ALL layers
    timeout_ms=30_000,       # 30-second wall clock limit
)

def summarize(prompt: str) -> dict:
    with ExecutionContext(config=config) as ctx:
        # Returns Decision.HALT if any limit is breached,
        # or the return value of call_llm() on success.
        result = ctx.wrap_llm_call(fn=lambda: call_llm(prompt))

        if result == Decision.HALT:
            # Limit breached. The LLM call was not dispatched,
            # so this blocked attempt adds no API cost.
            snapshot = ctx.get_snapshot()
            return {
                "status": "degraded",
                "reason": snapshot.abort_reason,
                "spent": f"${snapshot.cost_usd_accumulated:.2f}",
            }

        return {"status": "ok", "result": result}
```

What this does:

- `max_cost_usd=0.10` -- the entire agent run cannot spend more than 10 cents, regardless of how many layers retry.
- `max_retries_total=5` -- total retries across all layers combined. Not per layer. Chain-wide.
- `max_steps=20` -- total successful API calls. Prevents infinite tool loops.
- `timeout_ms=30_000` -- wall-clock hard stop after 30 seconds.

When any limit is hit, `wrap_llm_call()` returns `Decision.HALT` **without dispatching the LLM call**. The blocked attempt itself adds no API cost.

On a stubbed call path (`benchmarks/bench_baseline_comparison.py` in the repo), the full policy check averages around 11 microseconds. Typical LLM calls take 500-5000ms, so the containment overhead is negligible in practice.

## Before/after

| | Before | After |
|---|--------|-------|
| Worst-case calls (3-layer retry) | 64 | 6 (1 + 5 retries) |
| Cost ceiling | None | $0.10 |
| Total retry tracking | No | Yes |
| Wall-clock timeout | No | Yes |
| Behavior when API is down | Burns 64 calls, then fails | Burns 5 retries, then stops |
| Code changes to agent logic | -- | None |

The agent logic doesn't change. `ExecutionContext` wraps the calls from the outside. Your retries still work -- they just can't exceed the chain budget.

## What this does not do

`veronica-core` is a cost and execution control library. It is not:

- **An output validator.** It doesn't check what the LLM says. Use Guardrails AI or NeMo Guardrails for that.
- **A content filter.** It doesn't block harmful outputs. That's a different problem.
- **A prompt engineering tool.** It doesn't modify your prompts.
- **A framework.** It wraps your existing LLM calls. It doesn't replace your agent framework or custom loop.
- **A latency optimizer.** It doesn't make calls faster.
- **A fix for bad prompts.** If your agent loops because the prompt is wrong, that's a prompt problem. This just caps the damage.

It controls *how many times* your agent calls the API and *how much money* it spends. That's it.

## Install

```bash
pip install veronica-core
```

Python 3.10+. No required dependencies beyond the standard library.

Optional extras:
```bash
pip install veronica-core[redis]   # Distributed budget tracking (multi-process)
```

Source: [github.com/amabito/veronica-core](https://github.com/amabito/veronica-core)

There's also a `BudgetEnforcer` for standalone budget tracking, a `CircuitBreaker` for failure isolation, and ASGI/WSGI middleware if you want per-request containment in a web app. The retry amplification example above is probably the simplest place to start.

## You can reproduce the benchmark

The benchmark script is in the repo. It uses stub LLM implementations -- no API keys, no network calls:

```bash
git clone https://github.com/amabito/veronica-core
cd veronica-core
pip install -e .
python benchmarks/bench_retry_amplification.py
```

The article uses the 64-call example because it matches the common "3 retries per layer" mental model: each layer makes 1 initial attempt + 3 retries = 4 attempts, so `4^3 = 64`.

The benchmark in the repo uses a simpler always-failing stub with a `3 x 3 x 3` retry loop, which produces 27 baseline calls. Same bug, different retry convention. The benchmark shows those 27 calls reduced to 3 contained calls with `max_retries_total=5`.

---

Retry amplification is not a new idea. What's missing in most LLM stacks is a hard budget that applies to the entire run, not just one call at a time.

If you want to see the failure mode without spending real money, run `python benchmarks/bench_retry_amplification.py` in the repo. No API key, no network calls, and it makes the bug obvious in a few seconds.
