# veronica-core

![PyPI](https://img.shields.io/pypi/v/veronica-core?label=PyPI&cacheSeconds=300)
![CI](https://img.shields.io/badge/tests-1501%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-blue)

Runtime containment for LLM systems. Enforce cost, step, and retry limits before the call reaches the model.

veronica-core is the kernel. [veronica](https://github.com/amabito/veronica) is the control plane.

```bash
pip install veronica-core
```

---

## Quickstart

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
# {"total_cost_usd": 0.12, "total_llm_calls": 3, ...}
```

SDK-level injection (no per-call changes):

```python
from veronica_core.patch import patch_openai
from veronica_core import veronica_guard

patch_openai()  # patches openai.chat.completions.create

@veronica_guard(max_cost_usd=1.0, max_steps=20)
def run_agent(prompt: str) -> str:
    from openai import OpenAI
    return OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    ).choices[0].message.content
```

---

## Features

- **Budget enforcement** -- hard cost ceiling per chain, HALT before the call is made
- **Step limits** -- bounded recursion depth per entity
- **Circuit breaker** -- per-entity fail counts, COOLDOWN state, configurable threshold
- **Distributed circuit breaker** -- Redis-backed cross-process failure isolation with Lua-atomic transitions
- **Failure classification** -- predicate-based exception filtering (ignore 400s, count 500s)
- **Token budget** -- cumulative output/total token ceiling with DEGRADE zone
- **Retry containment** -- amplification control with jitter and backoff
- **Adaptive ceiling** -- auto-adjusts budget based on SafetyEvent history
- **Time-aware policy** -- weekend/off-hours budget multipliers
- **Semantic loop detection** -- word-level Jaccard similarity, no ML dependencies
- **Input compression** -- gates oversized inputs before they reach the model
- **Execution graph** -- typed node lifecycle, amplification metrics, divergence detection
- **Degradation ladder** -- 4-tier graceful degradation (model_downgrade, context_trim, rate_limit, halt)
- **Multi-agent context** -- parent-child ExecutionContext hierarchy with cost propagation
- **SafetyEvent** -- structured evidence for every non-ALLOW decision (SHA-256 hashed, no raw prompts)
- **Security containment** -- PolicyEngine, AuditLog, ed25519 policy signing, red-team regression suite
- **ASGI/WSGI middleware** -- per-request ExecutionContext via ContextVar, 429 on HALT
- **Auto cost calculation** -- pricing table for OpenAI, Anthropic, Google models

No required dependencies. Works with any LLM provider.

---

## Integrations

| Framework | Adapter | Example |
|-----------|---------|---------|
| OpenAI SDK | `patch_openai()` | [examples/integrations/openai_sdk/](examples/integrations/openai_sdk/) |
| Anthropic SDK | `patch_anthropic()` | -- |
| LangChain | `VeronicaCallbackHandler` | [examples/integrations/langchain/](examples/integrations/langchain/) |
| AG2 (AutoGen) | `CircuitBreakerCapability` | [examples/ag2_circuit_breaker.py](examples/ag2_circuit_breaker.py) |
| LlamaIndex | `VeronicaLlamaIndexHandler` | -- |
| CrewAI | decorator injection | [examples/integrations/crewai/](examples/integrations/crewai/) |
| ASGI/WSGI | `VeronicaASGIMiddleware` | [docs/middleware.md](docs/middleware.md) |

veronica-core integrates with [AG2](https://github.com/ag2ai/ag2) via `AgentCapability`. `CircuitBreakerCapability` wraps AG2 agents with failure detection and automatic recovery.

Working example: [PR #2430](https://github.com/ag2ai/ag2/pull/2430)

Current integration uses monkey-patching as AG2 does not yet expose before/after hooks on `generate_reply`. See the PR thread for context.

---

## Examples

| File | Description |
|------|-------------|
| [basic_usage.py](examples/basic_usage.py) | Budget enforcement and step limits |
| [execution_context_demo.py](examples/execution_context_demo.py) | Step limit, budget, abort, circuit, divergence |
| [adaptive_demo.py](examples/adaptive_demo.py) | Adaptive ceiling, cooldown, direction lock, anomaly, replay |
| [ag2_circuit_breaker.py](examples/ag2_circuit_breaker.py) | AG2 agent-level circuit breaker |
| [runaway_loop_demo.py](examples/runaway_loop_demo.py) | Runaway execution containment |
| [budget_degrade_demo.py](examples/budget_degrade_demo.py) | DEGRADE before HALT |
| [token_budget_minimal_demo.py](examples/token_budget_minimal_demo.py) | Token ceiling enforcement |

---

## Architecture

```
Application / Agent Framework
         |
    veronica-core          <-- enforcement boundary
         |
    LLM Provider (OpenAI, Anthropic, etc.)
```

Each call passes through a `ShieldPipeline` of registered hooks. Any hook may emit `DEGRADE` or `HALT`. A `HALT` blocks the call and emits a `SafetyEvent`. The caller receives the decision.

veronica-core does not prescribe how the caller handles `DEGRADE` or `HALT`. It enforces that the evaluation occurs, the decision is recorded, and the call does not proceed past `HALT`.

For detailed architecture, see [docs/architecture.md](docs/architecture.md).

---

## Security

Policy enforcement is at the process boundary (argv-level). This is not an OS-level sandbox.

Includes a 20-scenario red-team regression suite covering exfiltration, credential hunt, workflow poisoning, and persistence attacks. All scenarios blocked on every CI run.

Details: [docs/SECURITY_CONTAINMENT_PLAN.md](docs/SECURITY_CONTAINMENT_PLAN.md) | [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | [docs/SECURITY_CLAIMS.md](docs/SECURITY_CLAIMS.md)

---

## Stats

1501 tests, 92% coverage, zero required dependencies. Python 3.10+.

---

## Roadmap

- Formal containment guarantee documentation
- `ExecutionGraph` extensibility hooks for external integrations
- `PlannerProtocol`: minimal Python Protocol defining the Planner/Executor contract


---

## Install

```bash
pip install veronica-core
```

Optional extras:

```bash
pip install veronica-core[redis]   # DistributedCircuitBreaker, RedisBudgetBackend
pip install veronica-core[otel]    # OpenTelemetry export
```

Development:

```bash
git clone https://github.com/amabito/veronica-core
cd veronica-core
pip install -e ".[dev]"
pytest
```

---

## Version History

See [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT
