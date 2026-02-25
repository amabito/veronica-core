# VERONICA

![PyPI](https://img.shields.io/pypi/v/veronica-core?label=PyPI)
![CI](https://img.shields.io/badge/tests-1289%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-blue)

**Runtime containment for LLM systems. Enforce cost, step, and retry limits before damage occurs.**

```bash
pip install veronica-core
```

---

## The Problem

Observability tells you that an agent spent $12,000 over a weekend. It records every call. It does not stop it.

Runtime containment stops it — before it happens, not after.

---

## 30-Second Demo

```python
# Option A: SDK-level (no per-call changes)
from veronica_core.patch import patch_openai
from veronica_core import veronica_guard, GuardConfig

patch_openai()  # patches openai.chat.completions.create

@veronica_guard(max_cost_usd=1.0, max_steps=20)
def run_agent(prompt: str) -> str:
    from openai import OpenAI
    return OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    ).choices[0].message.content
```

```python
# Option B: Explicit boundary
from veronica_core.containment import ExecutionContext, ExecutionConfig

with ExecutionContext(config=ExecutionConfig(
    max_cost_usd=1.00,
    max_steps=50,
)) as ctx:
    decision = ctx.wrap_llm_call(fn=agent_step)
    # Decision.HALT if any limit exceeded -- fn is never called
```

---

## Table of Contents

1. [The Missing Layer in LLM Stacks](#1-the-missing-layer-in-llm-stacks)
2. [Why LLM Calls Are Not APIs](#2-why-llm-calls-are-not-apis)
3. [What Runtime Containment Means](#3-what-runtime-containment-means)
4. [Containment Layers](#4-containment-layers-in-veronica)
5. [Architecture Overview](#5-architecture-overview)
6. [Security Boundary](#security-boundary)
7. [OSS and Cloud Boundary](#6-oss-and-cloud-boundary)
8. [Design Philosophy](#7-design-philosophy)
9. [Quickstart](#quickstart-5-minutes)
10. [AIcontainer](#aicontainer-v092)
11. [veronica_guard](#veronica_guard--decorator-injection-v093)
12. [patch_openai / patch_anthropic](#patch_openai--patch_anthropic--automatic-sdk-injection-v094)
13. [VeronicaCallbackHandler](#veronicacallbackhandler--langchain-integration-v095)
14. [SemanticLoopGuard](#semanticloopguard--semantic-loop-detection-v096)
15. [Limits & Defaults](#limits--defaults)
16. [Security](#security)
17. [Red Team Regression](#red-team-regression)
18. [Security Guarantees](#security-guarantees)
19. [Roadmap](#roadmap)
20. [Install](#install)
21. [Version History](#version-history)
22. [License](#license)

---

## 1. The Missing Layer in LLM Stacks

Modern LLM stacks are built around three well-understood components:

- **Prompting** — instruction construction, context management, few-shot formatting
- **Orchestration** — agent routing, tool dispatch, workflow sequencing
- **Observability** — tracing, logging, cost dashboards, latency metrics

What they lack is a fourth component: **runtime containment**.

Observability != Containment.

An observability stack tells you that an agent spent $12,000 over a weekend. It records the retry loops, the token volumes, the timestamp of each failed call. It produces a precise audit trail of a runaway execution.

What it does not do is stop it.

Runtime containment is the component that stops it. It operates before the damage occurs, not after. It enforces structural limits on what an LLM-integrated system is permitted to do at runtime — independent of prompt design, orchestration logic, or model behavior.

---

## 2. Why LLM Calls Are Not APIs

LLM calls are frequently treated as ordinary API calls: send a request, receive a response. This framing is incorrect, and the gap between the two creates reliability problems at scale.

Standard API calls exhibit predictable properties:
- Deterministic behavior for identical inputs
- Fixed or bounded response cost
- Safe retry semantics (idempotent by construction)
- No recursive invocation patterns

LLM calls exhibit none of these:

**Stochastic behavior.** The same prompt produces different outputs across invocations. There is no stable function to test against. Every call is a sample from a distribution, not a deterministic computation.

**Variable token cost.** Output length is model-determined, not caller-determined. A single call can consume 4 tokens or 4,000. Budget projections based on typical behavior fail under adversarial or unusual inputs.

**Recursive invocation.** Agents invoke tools; tools invoke agents; agents invoke agents. Recursion depth is not bounded by the model itself. A single top-level call can spawn hundreds of descendant calls with no inherent termination condition.

**Retry amplification.** When a component fails under load, exponential backoff retries compound across nested call chains. A failure rate of 5% per layer, across three layers, does not produce a 15% aggregate failure rate — it produces amplified retry storms that collapse throughput.

**Non-idempotent retries.** Retrying an LLM call is not guaranteed to be safe. Downstream state mutations, external tool calls, and partial execution all make naive retry semantics dangerous.

LLM calls are probabilistic, cost-generating components. They require structural bounding. They cannot be treated as deterministic, cost-stable services.

---

## 3. What Runtime Containment Means

Runtime containment is a constraint layer that enforces bounded behavior on LLM-integrated systems.

It does not modify prompts. It does not filter content. It does not evaluate output quality. It enforces operational limits on the execution environment itself — evaluated at call time, before the model is invoked.

A runtime containment layer enforces:

1. **Bounded cost** — maximum token spend and call volume per window, per entity, per system
2. **Bounded retries** — rate limits and amplification controls that prevent retry storms from escalating
3. **Bounded recursion** — per-entity circuit-breaking that terminates runaway loops regardless of orchestration logic
4. **Bounded wait states** — isolation of stalled or degraded components from the rest of the system
5. **Failure domain isolation** — structural separation between a failing component and adjacent components, with auditable evidence

VERONICA implements these five properties as composable, opt-in primitives.

---

## 4. Containment Layers in VERONICA

### Layer 1 — Cost Bounding

In distributed systems, resource quotas enforce hard limits on consumption per tenant, per service, per time window. Without them, a single runaway process exhausts shared resources.

LLM systems face the same problem at the token and call level. Without cost bounding, a single agent session can consume unbounded token volume with no mechanism to stop it.

VERONICA components:

- **BudgetWindowHook** — enforces a call-count ceiling within a sliding time window; emits DEGRADE before the ceiling is reached, then HALT at the ceiling
- **TokenBudgetHook** — enforces a cumulative token ceiling (output tokens or total tokens) with a configurable DEGRADE zone approaching the limit
- **TimeAwarePolicy** — applies time-based multipliers (off-hours, weekends) to reduce active ceilings during periods of lower oversight
- **AdaptiveBudgetHook** — adjusts ceilings dynamically based on observed SafetyEvent history; stabilized with cooldown windows, per-step smoothing, hard floor and ceiling bounds, and direction lock

---

### Layer 2 — Amplification Control

In distributed systems, retry amplification is a well-documented failure mode: a component under pressure receives more retries than it can handle, which increases pressure, which triggers more retries. Circuit breakers and rate limiters exist to interrupt this dynamic.

LLM systems exhibit the same failure mode. A transient model error triggers orchestration retries. Each retry may invoke tools, which invoke the model again. The amplification is geometric.

VERONICA components:

- **BudgetWindowHook** — the primary amplification control; a ceiling breach halts further calls regardless of upstream retry logic or backoff strategy
- **DEGRADE decision** — signals fallback behavior before hard stop, allowing graceful degradation (e.g., model downgrade) rather than binary failure
- **Anomaly tightening** (AdaptiveBudgetHook) — detects spike patterns in SafetyEvent history and temporarily reduces the effective ceiling during burst activity, with automatic recovery when the burst subsides

---

### Layer 3 — Recursive Containment

In distributed systems, recursive or cyclic call graphs require depth bounds or visited-node tracking to prevent infinite traversal. Without them, any recursive structure is a potential infinite loop.

LLM agents are recursive by construction: tool calls invoke the model; the model invokes tools. The recursion is implicit in the orchestration design, not explicit in any single call.

VERONICA components:

- **VeronicaStateMachine** — tracks per-entity fail counts; activates COOLDOWN state after a configurable number of consecutive failures; transitions to SAFE_MODE for system-wide halt
- **Per-entity cooldown isolation** — an entity in COOLDOWN is blocked from further invocations for a configurable duration; this prevents tight loops on failing components without affecting other entities
- **ShieldPipeline** — composable pre-dispatch hook chain; all registered hooks are evaluated in order before each LLM call; any hook may emit DEGRADE or HALT

---

### Layer 4 — Stall Isolation

In distributed systems, a stalled downstream service causes upstream callers to block on connection pools, exhaust timeouts, and degrade responsiveness across unrelated request paths. Bulkhead patterns and timeouts exist to contain stall propagation.

LLM systems stall when a model enters a state of repeated low-quality, excessively verbose, or non-terminating responses. Without isolation, a stalled model session propagates degradation upstream.

VERONICA components:

- **VeronicaGuard** — abstract interface for domain-specific stall detection; implementations inspect latency, error rate, response quality, or any domain signal to trigger immediate cooldown activation, bypassing the default fail-count threshold
- **Per-entity cooldown** (VeronicaStateMachine) — stall isolation is per entity; a stalled tool or agent does not trigger cooldown for entities with clean histories
- **MinimalResponsePolicy** — opt-in system-message injection that enforces output conciseness constraints, reducing the probability of runaway token generation from verbose model states

---

### Layer 5 — Failure Domain Isolation

In distributed systems, failure domain isolation ensures that a fault in one component does not propagate to adjacent components. Structured error events, circuit-state export, and tiered shutdown protocols are standard mechanisms for this.

LLM systems require the same. A component failure should produce structured evidence, enable state inspection, and permit controlled shutdown without corrupting adjacent execution state.

VERONICA components:

- **SafetyEvent** — structured evidence record for every non-ALLOW decision; contains event type, decision, hook identity, and SHA-256 hashed context; raw prompt content is never stored
- **Deterministic replay** — control state (ceiling, multipliers, adjustment history) can be exported and re-imported; enables observability dashboard integration and post-incident reproduction
- **InputCompressionHook** — gates oversized inputs before they reach the model; HALT on inputs exceeding the ceiling, DEGRADE with compression recommendation in the intermediate zone
- **VeronicaExit** — three-tier shutdown protocol (GRACEFUL, EMERGENCY, FORCE) with SIGTERM and SIGINT signal handling and atexit fallback; state is preserved where possible at each tier

---

## 5. Architecture Overview

VERONICA operates as a middleware constraint layer between the orchestration layer and the LLM provider. It does not modify orchestration logic. It enforces constraints on what the orchestration layer is permitted to dispatch downstream.

```
App
  |
  v
Orchestrator
  |
  v
Runtime Containment (VERONICA)
  |
  v
LLM Provider
```

Each call from the orchestrator passes through the ShieldPipeline before reaching the provider. The pipeline evaluates registered hooks in order. Any hook may emit DEGRADE or HALT. A HALT decision terminates the call and emits a SafetyEvent. The orchestrator receives the decision and handles it according to its own logic.

VERONICA does not prescribe how the orchestrator responds to DEGRADE or HALT. It enforces that the constraint evaluation occurs, that the decision is recorded as a structured event, and that the call does not proceed past a HALT decision.

---

## Security Boundary

veronica-core enforces execution policy at the process boundary (argv-level). It is not an OS-level sandbox.

**What veronica-core does NOT guarantee:**

- Does not contain subprocesses spawned by allowed binaries (e.g., a build tool invoking a subshell)
- Does not restrict syscalls
- Does not enforce kernel-level or container-level isolation
- Does not inspect the content of LLM responses

**What veronica-core DOES guarantee:**

- Cost containment: hard ceilings on token spend and call volume
- Retry containment: amplification control and circuit breaking
- Step limits: bounded recursion depth per entity
- Fail-closed policy enforcement: a policy file that exists but cannot be parsed raises `RuntimeError`; unknown or unevaluated actions default to DENY

**On build tools and subshells:**

If a binary such as `make` spawns a subshell internally, that execution occurs outside veronica-core's policy scope. PolicyEngine inspects the argv at the point of call; it has no visibility into child processes created by the called binary. This is why build tools are not allowlisted by default.

---

## 6. OSS and Cloud Boundary

**veronica-core** is the local containment primitive library. It contains all enforcement logic: ShieldPipeline, BudgetWindowHook, TokenBudgetHook, AdaptiveBudgetHook, TimeAwarePolicy, InputCompressionHook, MinimalResponsePolicy, VeronicaStateMachine, SafetyEvent, VeronicaExit, and associated state management.

veronica-core operates without network connectivity, external services, or vendor dependencies. All containment decisions are local and synchronous.

**veronica-cloud** (forthcoming) provides coordination primitives for multi-agent and multi-tenant deployments: shared budget pools, distributed policy enforcement, and real-time dashboard integration for SafetyEvent streams.

The boundary is functional: cloud enhances visibility and coordination across distributed deployments. It does not enhance safety. Safety properties are enforced by veronica-core at the local layer. An agent running without cloud connectivity is still bounded. An agent running without veronica-core is not.

---

## 7. Design Philosophy

VERONICA is not:

- **Observability** — it does not trace, log, or visualize execution after the fact
- **Content guardrails** — it does not inspect, classify, or filter prompt or completion content
- **Evaluation tooling** — it does not assess output quality, factual accuracy, or alignment properties

VERONICA is:

- **Runtime constraint enforcement** — hard and soft limits on call volume, token spend, input size, and execution state, evaluated before each LLM call
- **Systems-level bounding layer** — structural containment at the orchestration boundary, treating LLM calls as probabilistic, cost-generating components that require bounding

The design is deliberately narrow. A component that attempts to solve observability, guardrails, containment, and evaluation simultaneously solves none of them well. VERONICA solves containment.

---

## Quickstart (5 minutes)

### Install

```bash
pip install veronica-core
```

### Minimal runtime containment example

```python
from veronica_core import ExecutionContext, ExecutionConfig, WrapOptions

def simulated_llm_call(prompt: str) -> str:
    return f"response to: {prompt}"

config = ExecutionConfig(
    max_cost_usd=1.00,    # hard cost ceiling per chain
    max_steps=50,         # hard step ceiling
    max_retries_total=10,
    timeout_ms=0,
)

with ExecutionContext(config=config) as ctx:
    for i in range(3):
        decision = ctx.wrap_llm_call(
            fn=lambda: simulated_llm_call(f"prompt {i}"),
            options=WrapOptions(
                operation_name=f"generate_{i}",
                cost_estimate_hint=0.04,
            ),
        )
        if decision.name == "HALT":
            break

snap = ctx.get_graph_snapshot()
print(snap["aggregates"])
```

### Expected output

```python
{
    "total_cost_usd": 0.12,
    "total_llm_calls": 3,
    "total_tool_calls": 0,
    "total_retries": 0,
    "max_depth": 1,
    "llm_calls_per_root": 3.0,
    "tool_calls_per_root": 0.0,
    "retries_per_root": 0.0,
    "divergence_emitted_count": 0
}
```

This demonstrates runtime containment as a structural property: every call is recorded
into an execution graph, amplification is measurable at the chain level, and HALT
semantics are deterministic and auditable per node.

### What each part does

- `ExecutionConfig` — declares hard limits for the chain (cost, steps, retries, timeout)
- `ExecutionContext` — scopes one agent run or request chain; enforces limits at dispatch time
- `wrap_llm_call()` — records the call as a typed node; evaluates all containment conditions before dispatch
- `get_graph_snapshot()` — returns an immutable, JSON-serializable view of the execution graph

### Enforce a step ceiling

```python
from veronica_core import ExecutionContext, ExecutionConfig, WrapOptions
from veronica_core.shield.types import Decision

config = ExecutionConfig(max_cost_usd=10.0, max_steps=5, max_retries_total=20, timeout_ms=0)

with ExecutionContext(config=config) as ctx:
    for i in range(10):
        decision = ctx.wrap_llm_call(
            fn=lambda: "result",
            options=WrapOptions(operation_name=f"step_{i}"),
        )
        if decision == Decision.HALT:
            print(f"Halted at step {i}")
            break
```

### What to read next

- [docs/execution-context.md](docs/execution-context.md) -- ExecutionContext API spec
- [docs/execution-graph.md](docs/execution-graph.md) -- execution graph model and invariants
- [docs/amplification-factor.md](docs/amplification-factor.md) -- amplification metrics
- [docs/divergence-heuristics.md](docs/divergence-heuristics.md) -- divergence detection
- [docs/adaptive-control.md](docs/adaptive-control.md) -- full engineering doc for adaptive ceiling control (v0.7.0)
- [docs/cookbook.md](docs/cookbook.md) -- copy-paste recipes for common patterns
- [examples/execution_context_demo.py](examples/execution_context_demo.py) -- runnable scenarios (step limit, budget, abort, circuit, divergence)
- [examples/adaptive_demo.py](examples/adaptive_demo.py) -- adaptive ceiling demo (cooldown, direction lock, anomaly, replay)
- [CHANGELOG.md](CHANGELOG.md) -- version history

---

**Records:** every LLM and tool call as a typed node in an execution graph.
**Never stores:** prompt contents. Evidence uses SHA-256 hashes by default.

---

## AIcontainer (v0.9.2)

`AIcontainer` is a declarative execution boundary that composes veronica-core primitives
into a single container object. Use it when you want to declare all boundaries upfront
instead of wiring primitives individually.

```python
from veronica_core.container import AIcontainer
from veronica_core import BudgetEnforcer, CircuitBreaker, RetryContainer

container = AIcontainer(
    budget=BudgetEnforcer(limit_usd=10.0),
    circuit_breaker=CircuitBreaker(failure_threshold=3),
    retry=RetryContainer(max_retries=2),
)

decision = container.check(cost_usd=0.5)
if not decision.allowed:
    raise RuntimeError(f"Boundary violated: {decision.reason}")

print(container.active_policies)  # ['budget', 'circuit_breaker', 'retry_budget']
```

All arguments are optional. Pass only the boundaries you need.
Existing imports (`from veronica_core import BudgetEnforcer`) are unchanged.

---

## veronica_guard — Decorator Injection (v0.9.3)

`veronica_guard` wraps any callable in an `AIcontainer` boundary without changing
the call site.

```python
from veronica_core.inject import veronica_guard, VeronicaHalt

@veronica_guard(max_cost_usd=1.0, max_steps=20, max_retries_total=3)
def call_llm(prompt: str) -> str:
    return llm.complete(prompt)

try:
    result = call_llm("Hello")
except VeronicaHalt as e:
    print(f"Denied: {e.reason}")
```

To return the `PolicyDecision` instead of raising:

```python
@veronica_guard(max_cost_usd=1.0, return_decision=True)
def call_llm(prompt: str):
    return llm.complete(prompt)

result = call_llm("Hello")
if isinstance(result, PolicyDecision):
    # policy denied -- handle gracefully
    ...
```

Use `is_guard_active()` to detect an active boundary from inside a call:

```python
from veronica_core.inject import is_guard_active

def my_tool():
    if is_guard_active():
        # running inside a veronica_guard boundary
        ...
```

---

## patch_openai / patch_anthropic — Automatic SDK Injection (v0.9.4)

Opt-in SDK patching applies `@veronica_guard` policies automatically to every
OpenAI or Anthropic API call made inside a guard boundary — no per-call changes
required.

```python
from veronica_core import veronica_guard
from veronica_core.patch import patch_openai

# Activate once at application startup.
# Safe to call if openai is not installed.
patch_openai()

@veronica_guard(max_cost_usd=1.0, max_steps=20)
def call_llm(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

# Budget is checked before the OpenAI call.
# Token cost is recorded against the budget after each response.
result = call_llm("Hello!")
```

**Guarantees:**
- Calls outside a `@veronica_guard` boundary pass through unchanged.
- Neither `openai` nor `anthropic` is a required dependency.
- `unpatch_all()` restores all originals (useful in tests).

---

## VeronicaCallbackHandler — LangChain Integration (v0.9.5)

Enforce VERONICA policies in LangChain pipelines via the standard callback interface.
No changes to existing call sites required.

```python
from langchain_openai import ChatOpenAI
from veronica_core.adapters.langchain import VeronicaCallbackHandler
from veronica_core import GuardConfig

handler = VeronicaCallbackHandler(GuardConfig(max_cost_usd=1.0, max_steps=20))
llm = ChatOpenAI(callbacks=[handler])

# Budget is checked before each LLM call.
# Token cost is recorded and steps counted after each response.
response = llm.invoke("Hello!")
```

Also works with `ExecutionConfig`:

```python
from veronica_core.containment import ExecutionConfig
handler = VeronicaCallbackHandler(
    ExecutionConfig(max_cost_usd=5.0, max_steps=50, max_retries_total=10)
)
```

**Guarantees:**
- `VeronicaHalt` raised on policy denial, halting the LangChain chain.
- Steps accumulate across the handler's lifetime (reset via `handler.container.reset()`).
- `langchain-core` or `langchain` must be installed separately.
- Importing `veronica_core` without langchain installed is safe.

---

## SemanticLoopGuard — Semantic Loop Detection (v0.9.6)

Detect when an LLM produces semantically repetitive outputs using pure-Python
word-level Jaccard similarity — no heavy ML dependencies required.

```python
from veronica_core import SemanticLoopGuard, AIcontainer

guard = SemanticLoopGuard(
    window=3,                # rolling window size
    jaccard_threshold=0.92,  # similarity above this -> deny
    min_chars=80,            # skip short outputs to avoid false positives
)

# Attach to AIcontainer
container = AIcontainer(semantic_guard=guard)

# Or use standalone
result = guard.feed("The answer is 42. " * 5)  # record + check
if not result.allowed:
    print(f"Loop detected: {result.reason}")
```

**How it works:**
- Maintains a rolling buffer of recent outputs (up to `window` entries)
- Normalizes text (lowercase, whitespace collapse) before comparison
- Exact-match shortcut for O(1) identical output detection
- Pairwise Jaccard similarity check on word frozensets
- Outputs shorter than `min_chars` characters are skipped

```python
# Manual record/check API
guard.record("first llm output here...")
guard.record("second llm output here...")
decision = guard.check()  # PolicyDecision(allowed=bool, ...)

# Reset the buffer
guard.reset()
```

---

## Limits & Defaults

Hard limits and default values enforced by veronica-core at runtime. All
values are module-level constants; they are not configurable at call-site
in v0.10.5.

**Partial buffer (`PartialResultBuffer`)**

- `max_chunks = 10,000` — maximum number of streaming chunks that can be
  appended before `PartialBufferOverflow(ValueError)` is raised.
- `max_bytes = 10 MB` — maximum cumulative UTF-8 byte size across all chunks
  before `PartialBufferOverflow(ValueError)` is raised.
- On overflow, the exception carries structured evidence fields
  (`total_bytes`, `kept_bytes`, `total_chunks`, `kept_chunks`,
  `truncation_point`). Already-appended chunks are preserved; the
  overflowing chunk is rejected. `to_dict()` includes `"truncated": true`.
  `PartialBufferOverflow` is a `ValueError` subclass — existing
  `except ValueError` handlers continue to catch it.

**SafetyEvent chain cap (`ExecutionContext`)**

- `max_events_per_chain = 1,000` — maximum SafetyEvents recorded per
  `ExecutionContext` instance.
- Drop policy: **newest-dropped**. Events recorded after the cap is reached
  are silently discarded; the first 1,000 events are retained. This prevents
  memory exhaustion from event-flooding callers while preserving the earliest
  evidence for post-mortem analysis.

**Retry jitter (`RetryContainer`)**

- `jitter = 0.25` (default) — 25% multiplicative jitter applied to every
  exponential-backoff delay: `delay = base * 2**attempt * (1 + uniform(-0.25, 0.25))`,
  clamped to `[0.0, backoff_max]`. Set `jitter=0.0` to disable. Without
  jitter, simultaneous agents produce synchronized retry bursts; the default
  prevents thundering herd on shared downstream services.

**TokenBudgetHook concurrency**

- Pending-reservation accounting (v0.10.5): `before_llm_call()` atomically
  reserves `ctx.tokens_out` / `ctx.tokens_in` inside the lock after all
  checks pass. A second concurrent caller projecting `_output_total +
  _pending_output + estimate >= max_output_tokens` receives `Decision.HALT`
  before issuing its LLM call. Call `release_reservation()` to cancel a
  reservation when the LLM call fails before tokens are consumed.
- `record_usage()` releases the reservation for the actual token count and
  adds to the running total atomically.

**Security scope**

- All policy enforcement is **argv-level** (argument inspection before
  subprocess launch). veronica-core is not an OS-level sandbox, does not
  use `seccomp`, `namespaces`, `cgroups`, or `ptrace`. A compromised process
  can still perform arbitrary syscalls. Use veronica-core for structured
  policy enforcement in cooperative (or lightly adversarial) agent
  environments; pair with OS-level isolation for full containment.

---

## Security

VERONICA's Security Containment Layer provides a fail-closed enforcement boundary that stops dangerous agent actions at the tool-dispatch and egress level — independently of any upper-layer system prompt or agent rules. It enforces controls against uncontrolled shell execution, sensitive file reads, unauthenticated outbound requests, CI workflow modifications, and risk accumulation leading to automatic SAFE_MODE transition. Policy files are HMAC-SHA256 and ed25519 signed; supply chain changes route to REQUIRE_APPROVAL; runtime attestation detects privilege escalation mid-session.

For full architecture details, audit findings coverage, capability profiles, and custom policy configuration, see [docs/SECURITY_CONTAINMENT_PLAN.md](docs/SECURITY_CONTAINMENT_PLAN.md).

---

## Red Team Regression

VERONICA includes a permanent regression suite of 20 attack scenarios covering
the most common techniques an adversarial agent or prompt-injected payload
would attempt.

Every scenario is blocked by a specific containment rule — the test suite
verifies this on every CI run.

```bash
uv run pytest tests/redteam/ -v
```

### Coverage

| Category           | Scenarios | Description                                          |
|--------------------|-----------|------------------------------------------------------|
| Exfiltration       | 5         | HTTP POST, base64/hex GET encoding, high-entropy query, long URL |
| Credential Hunt    | 5         | `.env`, `.npmrc`, `id_rsa`, `.pem`, git credential helper |
| Workflow Poisoning | 5         | CI file write, git push, npm token, pip config, exec() bypass |
| Persistence        | 5         | Shell destruction, token replay, expired token, scope mismatch, sandbox traversal |

All 20 scenarios: **blocked**.

For the full scenario table, rule IDs, and architecture details, see
[docs/SECURITY_CONTAINMENT_PLAN.md#phase-f](docs/SECURITY_CONTAINMENT_PLAN.md#phase-f-red-team-regression).

---

## Security Guarantees

The following guarantees are verified by the VERONICA test suite on every CI
run. The full verifiable claim set is documented in
[docs/SECURITY_CLAIMS.md](docs/SECURITY_CLAIMS.md).

### Containment (20 red-team scenarios — all blocked)

| Category | Claims | Pytest coverage |
|----------|--------|-----------------|
| Exfiltration | HTTP POST, base64/hex encoding, high-entropy query, long URL | `tests/redteam/` |
| Credential Hunt | `.env`, SSH keys, `.pem`, npm/pip tokens | `tests/redteam/` |
| Workflow Poisoning | CI file write, git push, exec() bypass | `tests/redteam/` |
| Persistence | Token replay, sandbox traversal, scope mismatch | `tests/redteam/` |

### Cryptographic Integrity

| Guarantee | Mechanism | Pytest mapping |
|-----------|-----------|----------------|
| Policy files are signed | Ed25519 (v2) + HMAC-SHA256 (v1 fallback) | `tests/security/test_policy_signing.py` |
| Public key is pinned | SHA-256 pin in `policies/key_pin.txt` | `tests/security/test_key_pin.py` |
| Policy rollback is detected | `RollbackGuard` checks `policy_version` monotonicity | `tests/security/test_policy_rollback.py` |
| Release artifacts are verified | `tools/verify_release.py` exits 0 | `tests/tools/test_release_tools.py` |
| AuditLog hash chain survives concurrent writes | 10-thread concurrent append, chain integrity verified | `tests/security/test_audit_log_thread_safety.py` |
| Aliased subprocess imports detected | AST linter catches `import subprocess as sp; sp.run(...)` | `tests/security/test_lint_no_raw_exec.py` |

### Threat Model Coverage

| Threat | Defence |
|--------|---------|
| Prompt-injected tool calls | PolicyEngine DENY rules |
| Supply chain compromise | SBOM diff gate + approval token |
| Key substitution | Key pinning + CI enforcement |
| Policy tampering | Ed25519 sig verification at load |
| Rollback attack | RollbackGuard monotonic version check |
| Privilege escalation | AttestationChecker mid-session anomaly |
| Aliased exec bypass (`import os as x; x.system(...)`) | AST linter alias detection |
| State backend corruption | JSONBackend graceful fallback on corrupted data |

Full threat model: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)

---

## Ship Readiness (v0.10.7)

- [x] BudgetWindow stops runaway execution (ceiling enforced)
- [x] SafetyEvent records structured evidence for non-ALLOW decisions
- [x] DEGRADE supported (fallback at threshold, HALT at ceiling)
- [x] TokenBudgetHook: cumulative output/total token ceiling with DEGRADE zone
- [x] MinimalResponsePolicy: opt-in conciseness constraints for system messages
- [x] InputCompressionHook: real compression with Compressor protocol + safety guarantees (v0.5.1)
- [x] AdaptiveBudgetHook: auto-adjusts ceiling based on SafetyEvent history (v0.6.0)
- [x] TimeAwarePolicy: weekend/off-hours budget multipliers (v0.6.0)
- [x] Adaptive stabilization: cooldown, smoothing, floor/ceiling, direction lock (v0.7.0)
- [x] Anomaly tightening: spike detection with temporary ceiling reduction (v0.7.0)
- [x] Deterministic replay: export/import control state for observability (v0.7.0)
- [x] ExecutionGraph: first-class runtime execution graph with typed node lifecycle (v0.9.0)
- [x] Amplification metrics: llm_calls_per_root, tool_calls_per_root, retries_per_root (v0.9.0)
- [x] Divergence heuristic: repeated-signature detection, warn-only, deduped (v0.9.0)
- [x] AIcontainer: declarative execution boundary composing all runtime primitives (v0.9.1)
- [x] PolicyEngine: declarative DENY/REQUIRE_APPROVAL/ALLOW rule set (v0.9.1)
- [x] AuditLog: append-only JSONL with SHA-256 hash chain + secret masking (v0.9.1)
- [x] Policy signing: HMAC-SHA256 + ed25519 tamper detection (v0.9.1)
- [x] CI: release workflow secrets guard fixed (v0.9.2)
- [x] veronica_guard: decorator-based injection with contextvars guard detection (v0.9.3)
- [x] patch_openai / patch_anthropic: opt-in SDK patching with guard-context awareness (v0.9.4)
- [x] VeronicaCallbackHandler: LangChain adapter with pre/post-call policy enforcement (v0.9.5)
- [x] SemanticLoopGuard: pure-Python word-level Jaccard loop detection, integrated into AIcontainer (v0.9.6)
- [x] Thread safety: all core modules fully Lock-protected (v0.9.7)
- [x] Security: key-pin comparison uses hmac.compare_digest (timing-attack resistant) (v0.9.7)
- [x] Resource safety: timeout watcher thread joined on context exit (v0.9.7)
- [x] Auto Cost Calculation: pricing table + response-object extraction for OpenAI/Anthropic/Google (v0.10.0)
- [x] Distributed Budget: Redis INCRBYFLOAT backend for cross-process cost coordination (v0.10.0)
- [x] OpenTelemetry Export: SafetyEvent → OTel span events, privacy-safe, opt-in (v0.10.0)
- [x] Degradation Ladder: 4-tier graceful degradation (model_downgrade → context_trim → rate_limit → halt) (v0.10.0)
- [x] Multi-agent Context Linking: parent-child ExecutionContext hierarchy with cost propagation (v0.10.0)
- [x] Security patch: dev-key warning, sandbox credential exclusion, NonceRegistry TTL eviction (v0.10.1)
- [x] Security hardening: exec-flag bypass closed, URL parser unified, threading fixes (v0.10.2)
- [x] Security: combined flag bypass, stdin exec path, pip via -m, fail-closed policy (v0.10.3)
- [x] Concurrency: atomic budget spend, CircuitBreaker isolation, per-invocation guard (v0.10.4)
- [x] Adversarial hardening: TokenBudgetHook TOCTOU fix, BudgetWindow boundary fix, frequency divergence, RetryContainer jitter, PartialBufferOverflow (v0.10.5)
- [x] Test suite quality overhaul: Classical Testing alignment, requirement-driven tests, async/E2E/fault-injection coverage, aliased import detection (v0.10.6)
- [x] PyPI metadata: license display fix, Beta status, AI classifier, expanded keywords, project URLs (v0.10.7)
- [x] PyPI auto-publish on GitHub Release
- [x] Everything is opt-in & non-breaking (default behavior unchanged)

1289 tests passing. Minimum production use-case: runaway containment + graceful degrade + auditable events + token budgets + input compression + adaptive ceiling + time-aware scheduling + anomaly detection + execution graph + divergence detection + security containment layer + semantic loop detection + auto cost estimation + distributed budget + OTel export + multi-agent chain containment.

---

## Roadmap

### v0.11 (planned)
- Middleware mode (ASGI/WSGI integration for request-scoped containment)
- Improved divergence heuristics (cost-rate detection, token-velocity windows)
- PartialResultBuffer integration with ExecutionContext event stream

### v1.0
- Stable `ExecutionContext` API with formal deprecation policy
- Formal containment guarantee documentation
- `ExecutionGraph` extensibility hooks for external integrations
- Multi-agent containment primitives (shared budget pools, cross-chain circuit breaker)

---

## Install

```bash
pip install veronica-core
```

**Development install (contributing):**
```bash
git clone https://github.com/amabito/veronica-core
pip install -e ".[dev]"
pytest
```

---

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 0.10.7 | 2026-02-25 | PyPI metadata: license fix, Beta status, AI classifier, expanded keywords, project URLs |
| 0.10.6 | 2026-02-25 | Test suite quality overhaul: Classical Testing alignment, 37 new behavioral tests, aliased import detection |
| 0.10.5 | 2026-02-23 | Adversarial hardening: TOCTOU fix, PartialBufferOverflow, frequency divergence, jitter, event cap |
| 0.10.4 | 2026-02-22 | Concurrency & isolation: atomic spend, CircuitBreaker isolation, per-invocation guard |
| 0.10.3 | 2026-02-22 | Combined flag bypass, stdin exec, pip via -m, fail-closed policy |
| 0.10.2 | 2026-02-21 | Shell exec-flag bypass, operator deny, URL parser, key rotation |
| 0.10.1 | 2026-02-20 | Dev-key warning, sandbox credentials, NonceRegistry TTL |
| 0.10.0 | 2026-02-19 | Auto cost, distributed budget (Redis), OTel export, degradation ladder, multi-agent |

Full history: [CHANGELOG.md](CHANGELOG.md)

---

## License

MIT

---

*Runtime Containment is the missing layer in LLM infrastructure.*
