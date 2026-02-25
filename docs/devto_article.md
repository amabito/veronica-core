---
title: "The $12K Weekend: What Nobody Tells You About LLM Agents in Production"
published: false
tags: python, ai, llm, opensource
description: "LLM agents have a structural problem that prompts and dashboards can't fix. Here's what's actually happening — and a containment layer that enforces limits before the call happens."
cover_image:
canonical_url:
---

An autonomous agent ran over a weekend. By Monday it had made 47,000 API calls.

No one set a budget ceiling. No one enforced a retry limit. The agent hit a transient API error, retried, hit another, retried again — and kept going for 60 hours because nothing told it to stop.

I spent the first hour convinced it was a billing bug.

This isn't a one-off. Simon Willison has documented the pattern. The r/MachineLearning thread from January had 800 upvotes. The numbers vary — $3K, $8K, $12K — but the shape is always the same: retry loop, no ceiling, nobody home.

---

## We tried the obvious things first

The instinct is better observability. Set up cost alerts. Wire up a dashboard. These are good things — I'm not arguing against them.

But an alert fires *after* the call happens. The call has already consumed tokens. The money is already spent. You're getting a notification about something that's over.

Then we tried retry libraries. Tenacity, backoff. These handle transient failures fine — if a call fails, wait and retry. The problem is they have no concept of a dollar ceiling. And if your process crashes mid-run and auto-recovers, the retry counter resets to zero. You're back to the beginning.

We spent two weeks on circuit breakers, which felt clever for a while. Trip the breaker, stop the runaway, done. Except: the breaker lives in process memory. Process dies, auto-recovery kicks in, breaker is gone. We'd solved the problem for the happy path and nothing else.

Provider spend limits have a different issue — they're per-account, not per call chain. They don't propagate across sub-agents. Agent A has a $1.00 limit and spawns Agent B, which independently racks up $8. The provider limit never triggers because $9 total is nowhere near your account ceiling. Agent A never knew.

The gap isn't subtle once you see it: nothing enforces bounded execution *before the call happens*, in a way that survives process restarts.

---

## Why this is harder than it sounds

LLM agents are probabilistic, cost-generating components inside systems expected to behave reliably. That's a hard contradiction and it doesn't resolve through better prompts or more careful orchestration — those operate at the wrong layer.

The analogy I kept coming back to (and resisting, because it sounds grandiose) is operating systems. An OS doesn't know what your application is *doing*. It just enforces the resource contract: this process gets X memory, Y CPU time, and when it's done, it's done. If the process tries to take more, the OS says no.

LLM systems don't have that. Every agent is running on the honor system.

What you actually need is something that enforces, at call time: this chain can spend at most $X, run for at most N steps, and if I crash and restart, those limits are still in effect. If I spawn a sub-agent, its costs count against my limit — not just its own.

---

## What we built

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

with ExecutionContext(config=ExecutionConfig(
    max_cost_usd=1.00,
    max_steps=50,
    max_retries_total=10,
    timeout_ms=30_000,
)) as ctx:
    decision = ctx.wrap_llm_call(fn=my_agent_step)
    # Returns Decision.HALT if any limit exceeded
    # fn is never called when halted — no network request, no spend
```

`wrap_llm_call` checks the budget *before* calling `fn`. If the ceiling is hit, it returns `Decision.HALT` and never makes the network request. Nothing gets spent.

The multi-agent case is where this gets genuinely useful:

```python
with ExecutionContext(config=ExecutionConfig(max_cost_usd=1.00)) as parent:
    with parent.spawn_child(max_cost_usd=0.50) as child:
        decision = child.wrap_llm_call(fn=sub_agent_step)
        # child spend counts against parent's $1.00
        # parent halts if cumulative cost exceeds $1.00
```

Agent B has its own $0.50 sub-limit. But whatever B spends also comes off A's $1.00. A halts before the chain blows past $1.00 through a path nobody was watching.

**The halt state problem**

The circuit breaker issue — state disappearing on restart — we solved with atomic disk writes:

```python
from veronica_core import VeronicaIntegration
from veronica_core.state import VeronicaState

veronica = VeronicaIntegration()
veronica.state.transition(VeronicaState.SAFE_MODE, "operator halt")
# Writes atomically to disk (tmp → rename)
# Survives kill -9. Auto-recovery does NOT clear it.
# Requires explicit .state.transition(VeronicaState.IDLE, ...) to resume
```

Write to tmp, rename to target — this survives `kill -9` because the rename is atomic at the filesystem level. Auto-recovery doesn't clear SAFE_MODE. You put it in SAFE_MODE because something went wrong; you should have to explicitly decide to resume.

**When you'd rather degrade than stop**

Hard halts aren't always right. Sometimes you want the system to keep running at reduced capacity:

```python
# 80% budget: downgrade to a cheaper model
# 85%: trim context
# 90%: rate limit between calls
# 100%: halt
```

Thresholds and model mappings are configurable.

**Across processes**

```python
config = ExecutionConfig(max_cost_usd=10.00, redis_url="redis://localhost:6379")
# Workers share one budget ceiling via Redis INCRBYFLOAT
```

---

## Numbers

This runs inside an autonomous trading system. 30 days continuous, 1,000+ ops/sec, 2.6M+ operations. During that time, 12 crashes — SIGTERM, SIGINT, one OOM kill. 100% recovery, no data loss.

The destruction tests are reproducible. You don't have to take our word for it:

```bash
git clone https://github.com/amabito/veronica-core
python scripts/proof_runner.py
```

SAFE_MODE persistence through kill -9, budget ceiling enforcement, child cost propagation. They pass or they don't.

---

## Install

```bash
pip install veronica-core
```

Zero external dependencies for core. `opentelemetry-sdk` optional for OTel export, `redis` optional for cross-process budget. Works with LangChain, AutoGen, CrewAI, or whatever you're building. MIT.

**GitHub**: [amabito/veronica-core](https://github.com/amabito/veronica-core)

---

If you've hit something like this in production — curious what the failure mode looked like and what you ended up doing about it.
