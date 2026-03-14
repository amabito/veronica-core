# VERONICA-core

![PyPI](https://img.shields.io/pypi/v/veronica-core?label=PyPI&cacheSeconds=60)
![CI](https://img.shields.io/badge/tests-6125%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

Your agent retries 3 times per layer. Three layers deep, that is 64 API calls from one user click. veronica-core caps that at whatever limit you set -- and halts the call before it reaches the model.

Runtime containment kernel for LLM agents. Not a prompt filter. Not a semantic guardrail. Resource enforcement: budget ceilings, step limits, retry caps, and circuit breakers.

```bash
pip install veronica-core
```

Zero required dependencies. Python 3.10+. Works with any LLM provider.

---

## What it enforces

- **Budget ceiling** -- hard cost cap per chain. HALT before overspend.
- **Step limit** -- bounded recursion depth. No infinite agent loops.
- **Retry containment** -- 3 layers x 4 retries = 64 calls -> capped at your limit. One line.
- **Circuit breaker** -- per-entity failure counting with automatic COOLDOWN (local or Redis-backed).
- **Degrade / HALT** -- 4-tier graceful degradation ladder. The call does not proceed past HALT.

Containment, not observability -- it doesn't inspect prompts or completions; it caps resource consumption. If you want to filter prompt content, use [guardrails-ai](https://github.com/guardrails-ai/guardrails) or [NeMo Guardrails](https://github.com/NVIDIA/NeMoGuardrails). If you want to cap cost, recursion, and retries before the model call -- this is the library.

---

## Quickstart

Two lines. Your existing agent code unchanged. Hard ceiling at $1.00:

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

Full control with `ExecutionContext`:

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

---

## Ecosystem

veronica-core is the in-process enforcement kernel. It runs inside your agent process and decides ALLOW, DEGRADE, or HALT before each model call.

[veronica](https://github.com/amabito/veronica-public) is the control plane: policy management, fleet coordination, and dashboard. It tells veronica-core what to enforce.

[TriMemory](https://github.com/amabito/tri-memory) resolves which document governs before inference; veronica-core enforces that the inference does not exceed its resource budget.

```python
# TriMemory resolves what the agent knows
knowledge_state = tri_memory_engine.compile(documents)

# veronica-core enforces that the inference stays within budget
@veronica_guard(max_cost_usd=0.50, max_steps=10)
def answer_query(query: str) -> str:
    context = knowledge_state.retrieve(query)
    return llm.complete(query, context=context)
```

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

AG2 integration via `AgentCapability`: [PR #2430](https://github.com/ag2ai/ag2/pull/2430) (merged)

---

## Architecture

![Architecture overview](docs/diagrams/architecture-overview.svg)

Each call passes through a `ShieldPipeline` of registered hooks. Any hook may emit `DEGRADE` or `HALT`. A `HALT` blocks the call and emits a `SafetyEvent`. veronica-core enforces that the evaluation occurs and the call does not proceed past `HALT`.

veronica-core does not schedule, route, or orchestrate agents. Policy management and fleet coordination belong to [veronica](https://github.com/amabito/veronica-public).

Details: [docs/architecture.md](docs/architecture.md) -- includes [supporting systems](docs/diagrams/supporting-systems.svg) and [evaluation flow](docs/diagrams/shield-pipeline-flow.svg) diagrams.

---

## Security

Process-boundary policy enforcement. 20-scenario red-team regression suite covering exfiltration, credential hunt, workflow poisoning, and persistence attacks. 4 rounds of independent security audit (130+ findings fixed).

Details: [docs/SECURITY_CONTAINMENT_PLAN.md](docs/SECURITY_CONTAINMENT_PLAN.md) | [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)

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
- **Two-phase budget** -- reserve/commit prevents double-spending across concurrent calls
- **Security containment** -- PolicyEngine, AuditLog, ed25519 signing, red-team regression suite
- **MCP containment** -- sync and async MCP server adapters with per-tool budget enforcement
- **Declarative policy** -- YAML/JSON policy files with hot-reload, 7 builtin rule types

Full feature list: [docs/FEATURES.md](docs/FEATURES.md)

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

## Stats

6131 tests, 94% coverage, zero required dependencies. Zero breaking changes from v2.1.0 through v3.7.4. v3.7.5 removes the deprecated `veronica_core.adapter` shim (deprecated since v3.4.0; use `veronica_core.adapters.exec` instead). Python 3.10+.

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

v4.0 Federation (multi-process policy coordination) is the next milestone. No timeline commitment -- veronica-core is stable at v3.7.6.

Full roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)

---

## License

Apache-2.0
