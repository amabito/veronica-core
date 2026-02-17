# Threat Model

## Purpose

This document describes what VERONICA protects, what it does not protect, how it fails, and the invariants that hold under normal and adversarial conditions. It is intended for engineers evaluating VERONICA as a runtime safety layer, operators deploying it in production, and security researchers assessing its design. VERONICA is a runtime execution enforcement system, not a general-purpose security framework. Understanding the boundaries of its protection is as important as understanding what it provides.

## Assets Protected

| Asset | Description |
|-------|-------------|
| **LLM API spend** | Budget enforcement (USD and token caps) prevents runaway costs from infinite loops, retry bugs, or unbounded agent execution. |
| **System availability** | The DegradeController monitors failure signals and progressively restricts execution before cascading failures reach downstream systems. |
| **Agent execution integrity** | Loop detection hooks allow the caller to halt agents stuck in repetitive execution patterns. |
| **Resource fairness** | The admission control scheduler (WFQ) prevents any single org or team from monopolizing concurrency slots, protecting throughput for other workloads. |
| **Operational state** | In-memory enforcement state (budget counters, circuit breaker state, scheduler queues) is maintained for the lifetime of the process. For persistent state that survives crashes, use veronica-cloud. |

## Trust Boundaries

```
+---------------------------------------------+
|  Caller (agent framework, application code)  |
|                                               |
|   Reports: cost_usd, tokens_in, tokens_out   |
|   Calls:   create_run, create_session,        |
|            llm_call, check_budget             |
+---------------------+-----------------------+
                       | VERONICA API boundary
+---------------------v-----------------------+
|  VERONICA Core                               |
|                                              |
|   Enforces: budgets, concurrency,            |
|             degradation control              |
|   Trusts:   OS clock (time.monotonic)        |
|   Does NOT trust: agent behavior             |
+---------------------+-----------------------+
                       |
+---------------------v-----------------------+
|  OS / Runtime                                |
|   Python process memory (in-memory state)    |
|   Event sinks: JSONL append (thread-safe)    |
+---------------------------------------------+
```

**VERONICA trusts the caller to report accurate cost and token data.** It does not intercept API calls at the network level. If a caller reports `cost_usd=0.0`, budget enforcement is blind to actual spend. This is a known architectural limitation documented in the Attack Surface section below.

**VERONICA does not trust agent behavior.** That is precisely why enforcement exists as a separate, caller-independent layer.

## Attack Surface

### 1. Budget Bypass via False Cost Reporting

- **Threat**: Caller reports `step.cost_usd = 0.0` or omits cost data entirely to evade budget limits.
- **Impact**: Budget enforcement is ineffective. Unlimited spend is possible.
- **Mitigation (roadmap)**: Integration with provider billing APIs (OpenAI usage API, Anthropic usage API) to cross-validate reported costs against ground truth.
- **Current state**: VERONICA relies on caller-provided cost data. This is a known limitation. Callers operating in good faith (the common case) are protected. Callers deliberately evading enforcement are not.
- **Deployment guidance**: For high-stakes enforcement, deploy an external billing reconciliation check independent of VERONICA.

### 2. In-Memory State Tampering

- **Threat**: A malicious module running in the same process modifies enforcement state objects (budget counters, breaker state) directly via Python attribute access.
- **Impact**: Enforcement state is corrupted. A halted run could appear active. Budget counters could be zeroed.
- **Mitigation**: Enforcement objects are standard Python dataclasses. No runtime memory protection is provided. This is consistent with Python's trust model: code running in the same process has full access.
- **Current state**: In-process trust boundary only. Effective when VERONICA is the sole enforcement layer in a trusted process. Ineffective against malicious code injected into the same process.
- **Deployment guidance**: Do not load untrusted plugins or agent code in the same process as VERONICA enforcement. For multi-tenant isolation, use veronica-cloud.

### 3. Event Sink Failure

- **Threat**: All registered event sinks (logging, alerting, metrics) fail simultaneously, causing enforcement events (BUDGET_EXCEEDED, RUN_HALTED) to be silently dropped.
- **Impact**: Operators do not receive enforcement notifications. The enforcement decision itself is unaffected.
- **Mitigation**: `EventBus` catches per-sink exceptions individually, logs a warning to stderr, and continues processing remaining sinks. Critically, enforcement decisions (HALT, REJECT) are executed **before** events are emitted. Sink failure does not reverse an enforcement decision.
- **Current state**: Enforcement is decoupled from notification. An operator may be unaware of a halt event, but the halt has occurred.

### 4. Scheduler Starvation

- **Threat**: A flood of high-priority (P0) submissions starves lower-priority (P1/P2) work indefinitely under the weighted fair queuing scheduler.
- **Impact**: P1/P2 workloads never execute. Effectively a denial of service for lower-priority teams.
- **Mitigation**: Built-in starvation detection tracks wait time per entry. Entries waiting beyond the starvation threshold (default: 30 seconds, configurable) receive automatic priority elevation. The P0 flood cannot hold lower-priority work back indefinitely.
- **Current state**: Starvation detection is active by default. The threshold is tunable per deployment.

### 5. Denial of Service via Queue Flooding

- **Threat**: An attacker or buggy agent submits queue entries at high volume to exhaust process memory.
- **Impact**: Memory exhaustion, process crash, unavailability for legitimate workloads.
- **Mitigation**: Hard capacity limits enforced at admission: `org_queue_capacity=10000`, `team_queue_capacity=2000`. Submissions beyond capacity receive an immediate `REJECT` response. Rejected entries are not queued and do not consume memory.
- **Current state**: Capacity limits are configurable. Operators should tune them to their workload profile.

## Failure Modes

### VERONICA Process Crash (SIGKILL, OOM, Power Loss)

- **State after crash**: All in-flight enforcement state is lost. Budget counters, circuit breaker state, and scheduler queues reset to initial values on restart.
- **Recovery on restart**: VERONICA starts with clean state. The operator must create new runs. Event sink logs (JSONL) written before the crash are preserved and can be used for post-mortem analysis.
- **Risk window**: All steps executed since process start have no persistent enforcement record beyond event sink logs.
- **Deployment guidance**: For crash-resilient enforcement, use veronica-cloud with its Postgres-backed state store.

### Budget Check Race Condition (Multi-Process Deployment)

- **State**: Two concurrent processes call `check_budget()` simultaneously. Both read `used_usd < limit_usd`. Both proceed. One additional LLM call beyond the limit executes.
- **Mitigation**: `check_budget()` is designed for single-process deployments where calls are sequential per run. Multi-process deployments require external coordination (Redis atomic increments, database row locks) to eliminate the race.
- **Risk**: Low in single-process deployments (the typical use case for VERONICA Core). Present in multi-process deployments without external coordination.
- **Deployment guidance**: Multi-process deployments should use veronica-cloud, which provides distributed enforcement with atomic budget operations.

### Degradation Level Flap

- **State**: A service operating near the budget utilization or error rate threshold causes the DegradeController to oscillate between degradation levels.
- **Impact**: Erratic enforcement behavior. Runs may be intermittently degraded and restored within seconds.
- **Mitigation**: The DegradeController implements hysteresis: escalation is immediate, but recovery requires a configurable stability window (`recovery_window_s`, default: 60 seconds) at the lower signal level before de-escalating. Recovery proceeds one level at a time (EMERGENCY -> HARD -> SOFT -> NORMAL), not directly to NORMAL.
- **Risk**: Controllable via configuration. Operators should set the recovery window based on observed baseline failure rates.

## What is Explicitly Out of Scope

The following are not enforced by VERONICA Core and are not security responsibilities of this project:

- **Prompt injection detection or prevention**: VERONICA does not inspect LLM inputs or outputs for adversarial content.
- **LLM output content filtering or validation**: Harmful, biased, or incorrect model outputs are not VERONICA's concern.
- **API key or credential management**: VERONICA stores no credentials and provides no secrets management.
- **Network-level security**: TLS termination, firewall rules, and DDoS protection are infrastructure concerns outside this scope.
- **Multi-tenancy isolation**: Strong tenant isolation (preventing one tenant from observing another's state) is provided by veronica-cloud, not this library.
- **Regulatory compliance**: SOC 2, HIPAA, GDPR, and similar frameworks require controls beyond runtime enforcement. VERONICA may contribute to a compliance posture but does not itself provide compliance.

## Assumptions

The following assumptions must hold for VERONICA's security properties to be valid:

1. **Trusted execution environment**: VERONICA runs in an environment not directly controlled by an attacker. It is not designed to run inside an attacker-controlled sandbox.
2. **Uncompromised Python runtime**: The Python interpreter and stdlib are not modified by an attacker.
3. **Caller good faith (or external verification)**: Callers report cost and token data accurately, or the deployment adds external cost verification.
4. **Monotonic clock**: `time.monotonic()` is provided by the OS and is not subject to backward jumps. Wall clock (`time.time()`) is used only for human-readable timestamps, not for enforcement timing decisions.

## Security Invariants

The following properties hold by design and must not be violated by future changes to the codebase:

1. **HALTED prevents return to RUNNING.** A run in `HALTED` state can only transition to `FAILED` or `CANCELED`. It cannot return to `RUNNING` or `DEGRADED`.
2. **Budget enforcement is synchronous.** `check_budget()` completes the check and state transition before returning. The calling code cannot begin a new step before the check has resolved.
3. **Scheduler REJECT is final.** An entry rejected due to queue capacity is not silently re-queued or retried. The caller receives the rejection and must decide how to proceed.
4. **Event append is thread-safe.** `JsonlFileSink` uses a `threading.Lock` to serialize event writes. Event data is not lost on concurrent access within a single process.
5. **Enforcement actions and event emission.** BudgetEnforcer applies state transitions before emitting threshold events. Sink failure does not reverse an enforcement decision. Note: the legacy `check_budget()` method emits `BUDGET_EXCEEDED` before transitioning state.

## Version History

| Date | Change |
|------|--------|
| 2026-02-17 | Initial threat model |
