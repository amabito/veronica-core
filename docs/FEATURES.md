# Features

Core containment features and extended capabilities of veronica-core.

## Core

- **Budget enforcement** -- hard cost ceiling per chain, HALT before the call
- **Step limits** -- bounded recursion depth per entity
- **Circuit breaker** -- per-entity failure counting with COOLDOWN state (local and Redis-backed)
- **Token budget** -- cumulative output/total token ceiling with DEGRADE zone
- **Retry containment** -- amplification control with jitter and backoff
- **Semantic loop detection** -- word-level Jaccard similarity, no ML dependencies
- **Execution graph** -- typed node lifecycle, amplification metrics, divergence detection
- **Degradation ladder** -- 4-tier graceful degradation (model_downgrade, context_trim, rate_limit, halt)
- **Two-phase budget** -- reserve/commit/rollback prevents double-spending across concurrent calls
- **Security containment** -- PolicyEngine, AuditLog, ed25519 signing, red-team regression suite
- **MCP containment** -- sync and async MCP server adapters with per-tool budget enforcement
- **Declarative policy** -- YAML/JSON policy files with hot-reload, 7 builtin rule types

## Extended

- **Distributed circuit breaker** -- Redis-backed cross-process failure isolation with Lua-atomic transitions
- **Failure classification** -- predicate-based exception filtering (ignore 400s, count 500s)
- **Adaptive ceiling** -- auto-adjusts budget based on SafetyEvent history
- **Time-aware policy** -- weekend/off-hours budget multipliers
- **Input compression** -- gates oversized inputs before they reach the model
- **Multi-agent context** -- parent-child ExecutionContext hierarchy with cost propagation
- **Async budget backends** -- `AsyncLocalBudgetBackend` and `AsyncRedisBudgetBackend` with native asyncio coordination
- **WebSocket containment** -- ASGI middleware enforces step limits on WebSocket connections with `close(1008)`
- **CancellationToken** -- parent/child propagation with upward cost enforcement
- **SafetyEvent** -- structured evidence for every non-ALLOW decision (SHA-256 hashed, no raw prompts)
- **ASGI/WSGI middleware** -- per-request ExecutionContext via ContextVar, 429 on HALT
- **Auto cost calculation** -- pricing table for OpenAI, Anthropic, Google models
- **Adaptive budget** -- burn-rate estimation, time-to-exhaustion escalation, spike detection via Z-score anomaly
- **Multi-tenant budget** -- hierarchical Organisation/Project/Team/Agent with ancestor-walk policy resolution
- **OTel feedback loop** -- ingest AG2/OpenLLMetry spans, per-agent metrics, declarative `MetricRule` thresholds
- **ExecutionGraph hooks** -- dynamic observer/subscriber registration, `NodeEvent` lifecycle events
- **Policy simulation** -- replay execution logs against policy configs for what-if analysis, OTel span import
- **Framework adapter metrics** -- `record_decision` and `record_tokens` emission across all 5 framework adapters
- **Adapter capabilities** -- `AdapterCapabilities` frozen dataclass; each adapter declares its features at runtime via `capabilities()`
- **Audit chain** -- `AuditChain` tamper-proof SHA-256 hash chain for safety events; append-only, thread-safe, exportable to JSON

No required dependencies. Works with any LLM provider.
