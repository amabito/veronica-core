---
title: "feat: OpenAI Agents SDK adapter"
labels: enhancement, adapter, openai
---

## Why

OpenAI Agents SDK (`openai-agents`) is among the most widely deployed agent frameworks. VERONICA-Core has `patch_openai()` for the base `openai` SDK but no dedicated adapter for the Agents SDK. Users who migrate from raw `openai` to the Agents SDK lose containment coverage.

The AG2 adapter (`VeronicaAG2Adapter`) is the established reference pattern. The OpenAI Agents SDK adapter should follow the same structure.

## Goal

An adapter that wraps `Agent` execution with `ExecutionContext` containment: budget enforcement, step limits, and circuit-breaker protection per agent run.

## Scope

- Adapter module at `veronica_core/adapters/openai_agents.py`
- `VeronicaOpenAIAgentsAdapter` class following AG2 adapter patterns
- Budget enforcement per run (tokens + cost)
- Step limit enforcement
- Circuit breaker integration
- `wrap_agent()` convenience function
- Example script in `examples/`
- Tests using mock runner (no live API calls)

## Non-goals

- Full Agents SDK feature parity (handoffs, tracing)
- Swarm compatibility
- Streaming response containment (deferred)

## Why now

Market coverage: AG2, LangChain, LlamaIndex adapters exist. OpenAI Agents is the remaining major framework. Adds it to the adapter matrix.

## Acceptance criteria

- [ ] `VeronicaOpenAIAgentsAdapter` in `veronica_core/adapters/openai_agents.py`
- [ ] `wrap_agent(agent, execution_context)` convenience function
- [ ] Budget enforcement halts on exceed
- [ ] Circuit breaker trips after N consecutive failures
- [ ] Mock-based unit tests (no live API calls required)
- [ ] Example script in `examples/openai_agents_example.py`
- [ ] `AdapterCapabilities` declared for the adapter
