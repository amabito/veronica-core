# Changelog

## 0.1.0 — 2026-02-17

Initial release.

### Added

- **Runtime model** — Run / Session / Step lifecycle with deterministic state machine.
- **Event system** — Typed `EventBus` with pluggable sinks (stdout, JSONL file, composite, reporter bridge).
- **Budget enforcement** — `Budget(limit_usd=X)` with manual `check_budget()` and pre-call `BudgetEnforcer` (reservation-based).
- **Budget cgroup** — Hierarchical budget limits per org / team / user with minute / hour / day windows.
- **Admission control** — `Scheduler` with ALLOW / QUEUE / REJECT decisions based on concurrency limits.
- **Weighted Fair Queue** — Priority-aware scheduling across orgs and teams. Starvation prevention via priority boost.
- **Degradation control** — `DegradeController` with four levels (NORMAL / SOFT / HARD / EMERGENCY). Model downgrade, token cap, tool blocking, LLM rejection.
- **max_steps enforcement (R1)** — `Session.max_steps` checked before every `llm_call()` and `tool_call()`. Exceeding the limit transitions the session to HALTED, emits `MAX_STEPS_EXCEEDED`, and raises `MaxStepsExceeded`.
- **loop_detection_on flag (R2)** — `Session.loop_detection_on` gates `record_loop_detected()`. When `False`, the method is a no-op (no event, no halt).
- **Demo scenarios** — Retry cascade, budget burn, tool hang, runaway agent with deterministic replay.

### Notes

- Pure Python. Zero runtime dependencies.
- Requires Python >= 3.10.
- API is alpha-stage and may change in 0.2.0.
