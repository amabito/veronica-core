# veronica-core

![PyPI](https://img.shields.io/pypi/v/veronica-core?label=PyPI&cacheSeconds=60)
![CI](https://img.shields.io/badge/tests-4844%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-blue)

Runtime containment for LLM agent systems.
Budget, step, retry, and circuit breaker enforcement -- evaluated before the call reaches the model.

veronica-core is the kernel: it enforces execution boundaries.
[veronica](https://github.com/amabito/veronica-public) is the control plane: policy management, fleet coordination, and dashboard.

Containment, not observability. VERONICA does not inspect prompts or completions.
It governs resource consumption -- cost, steps, retries, timeouts, circuit state -- and halts calls that exceed policy.

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

patch_openai()

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

- **Budget enforcement** -- hard cost ceiling per chain, HALT before the call
- **Step limits** -- bounded recursion depth per entity
- **Circuit breaker** -- per-entity failure counting with COOLDOWN state (local and Redis-backed)
- **Token budget** -- cumulative output/total token ceiling with DEGRADE zone
- **Retry containment** -- amplification control with jitter and backoff
- **Semantic loop detection** -- word-level Jaccard similarity, no ML dependencies
- **Execution graph** -- typed node lifecycle, amplification metrics
- **Degradation ladder** -- 4-tier graceful degradation (model_downgrade, context_trim, rate_limit, halt)
- **Two-phase budget** -- reserve/commit/rollback prevents double-spending across concurrent calls
- **Security containment** -- PolicyEngine, AuditLog, ed25519 signing, red-team regression suite
- **MCP containment** -- sync and async MCP server adapters with per-tool budget enforcement
- **Declarative policy** -- YAML/JSON policy files with hot-reload, 7 builtin rule types

No required dependencies. Works with any LLM provider.

Full feature list: [docs/FEATURES.md](docs/FEATURES.md)

---

## Integrations

| Framework | Adapter | Example |
|-----------|---------|---------|
| OpenAI SDK | `patch_openai()` | [examples/integrations/openai_sdk/](examples/integrations/openai_sdk/) |
| Anthropic SDK | `patch_anthropic()` | -- |
| LangChain | `VeronicaCallbackHandler` | [examples/integrations/langchain/](examples/integrations/langchain/) |
| AG2 (AutoGen) | `CircuitBreakerCapability` | [examples/ag2_circuit_breaker.py](examples/ag2_circuit_breaker.py) |
| LlamaIndex | `VeronicaLlamaIndexHandler` | -- |
| CrewAI | `VeronicaCrewAIListener` | [examples/integrations/crewai/](examples/integrations/crewai/) |
| LangGraph | `VeronicaLangGraphCallback` | [examples/langgraph_minimal.py](examples/langgraph_minimal.py) |
| ASGI/WSGI | `VeronicaASGIMiddleware` | [docs/middleware.md](docs/middleware.md) |
| MCP | `MCPContainmentAdapter` | -- |
| ROS2 | `SafetyMonitor` | [examples/ros2/](examples/ros2/) |

AG2 integration via `AgentCapability`: [PR #2430](https://github.com/ag2ai/ag2/pull/2430) (merged)
AG2 `AgentEligibilityPolicy` for runtime GroupChat filtering: [PR #2459](https://github.com/ag2ai/ag2/pull/2459)

---

## Examples

| File | Description |
|------|-------------|
| [basic_usage.py](examples/basic_usage.py) | Budget enforcement and step limits |
| [execution_context_demo.py](examples/execution_context_demo.py) | Step limit, budget, abort, circuit, divergence |
| [adaptive_demo.py](examples/adaptive_demo.py) | Adaptive ceiling, cooldown, anomaly, replay |
| [ag2_circuit_breaker.py](examples/ag2_circuit_breaker.py) | AG2 agent-level circuit breaker |
| [langchain_minimal.py](examples/langchain_minimal.py) | LangChain integration quickstart |
| [langgraph_minimal.py](examples/langgraph_minimal.py) | LangGraph integration quickstart |

---

## Architecture

```
Application / Agent Framework
         |
    veronica-core          <-- enforcement boundary
         |
    LLM Provider (OpenAI, Anthropic, etc.)
```

Each call passes through a `ShieldPipeline` of registered hooks. Any hook may emit `DEGRADE` or `HALT`. A `HALT` blocks the call and emits a `SafetyEvent`. veronica-core enforces that the evaluation occurs and the call does not proceed past `HALT`.

veronica-core does not schedule, route, or orchestrate agents. Policy management and fleet coordination belong to [veronica](https://github.com/amabito/veronica).

Details: [docs/architecture.md](docs/architecture.md)

---

## Security

Process-boundary policy enforcement. 20-scenario red-team regression suite covering exfiltration, credential hunt, workflow poisoning, and persistence attacks. 4 rounds of independent security audit (130+ findings fixed).

Details: [docs/SECURITY_CONTAINMENT_PLAN.md](docs/SECURITY_CONTAINMENT_PLAN.md) | [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)

---

## Stats

4844 tests, 94% coverage, zero required dependencies. Zero breaking changes from v2.1.0 through v3.4.2. Python 3.10+.

Evaluation: [docs/EVALUATION.md](docs/EVALUATION.md) | [CHANGELOG.md](CHANGELOG.md)

---

## Install

```bash
pip install veronica-core
```

Optional extras:

```bash
pip install veronica-core[redis]   # DistributedCircuitBreaker, RedisBudgetBackend
pip install veronica-core[otel]    # OpenTelemetry export
pip install veronica-core[vault]   # VaultKeyProvider (HashiCorp Vault)
```

Development:

```bash
git clone https://github.com/amabito/veronica-core
cd veronica-core
pip install -e ".[dev]"
pytest
```

---

## Roadmap

v4.0 Federation (multi-process policy coordination) is the next milestone. No timeline commitment -- veronica-core is stable at v3.4.2.

Full roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)

---

## License

MIT
