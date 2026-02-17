# Roadmap

Development priorities for VERONICA Core. Updated as work progresses.

## Now (v0.x) -- Current

- [x] Runtime hooks: llm_call(), tool_call() context managers
- [x] Budget enforcement: USD and token limits with HALT
- [x] Circuit breaker: open/half_open/closed state transitions
- [x] Event bus: structured events with pluggable sinks
- [x] Scheduler: admission control with Weighted Fair Queue
- [x] Loop detection: session-level infinite loop prevention
- [x] State machine: Run/Session/Step with strict transitions
- [x] Priority scheduling: P0/P1/P2 with starvation prevention
- [x] Zero external dependencies
- [ ] PyPI package published
- [ ] 90%+ test coverage with CI enforcement
- [ ] Property-based tests for state machine transitions
- [ ] Benchmark suite with reproducible results

## Next (v1.0) -- Planned

- [ ] OpenTelemetry export sink (optional dependency)
- [ ] Redis-backed state persistence (optional dependency)
- [ ] Webhook alert sink: Slack, PagerDuty, OpsGenie
- [ ] Provider cost verification: cross-check reported costs with billing APIs
- [ ] Configurable policy engine: YAML/JSON policy definitions
- [ ] Session replay: reconstruct execution from event stream
- [ ] Multi-process budget coordination (file lock or Redis)

## Later -- Under Consideration

- [ ] Content policy hooks: pre/post LLM call inspection points
- [ ] Terraform provider for policy-as-code
- [ ] gRPC event transport for high-throughput deployments
- [ ] Dashboard integration protocol (for veronica-cloud)
- [ ] Compliance reporting templates (SOC 2, ISO 27001)

## Non-Goals

These are explicitly out of scope for VERONICA Core:

- Prompt injection detection (use Guardrails AI, Rebuff, or similar)
- LLM output content filtering (use NeMo Guardrails or similar)
- Model serving or inference (use vLLM, TGI, or similar)
- Observability platform (use Langfuse, LangSmith, or similar)

VERONICA is enforcement. It complements, not replaces, these tools.
