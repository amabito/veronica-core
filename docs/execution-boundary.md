# Execution Boundary

## Definition

The Execution Boundary is the point before an LLM call where a Decision (ALLOW / RETRY / DEGRADE / QUEUE / HALT) is made.
It is the enforcement surface, not the observation surface.
Decisions made here prevent the call from reaching the model provider at all.

---

## Observability vs Enforcement

|                        | Observability       | Enforcement (Execution Boundary) |
|------------------------|---------------------|----------------------------------|
| When                   | After the call      | Before the call                  |
| Prevents cost          | No                  | Yes                              |
| Stops runaway          | No                  | Yes                              |
| Circuit breaker        | No                  | Yes                              |

Observability records what happened. The Execution Boundary decides whether it happens.

---

## Why This Matters

Three failure modes that observability cannot prevent:

**Retry cascade**: An agent encounters a transient error and retries with the same arguments.
Without an enforcement boundary, the retry loop continues indefinitely.
The budget burns while no human is watching.

**Runaway agent**: Budget signals are emitted but nothing stops the next call.
Observability dashboards show the spend increasing.
Nothing intervenes until a human manually kills the process.

**Tool hang**: A tool call blocks waiting for a response that never arrives.
Without a timeout enforced at the boundary, the agent thread hangs.
Other work queues behind it.

All three are stopped at the Execution Boundary, not after the fact.

---

## VERONICA's Approach

VERONICA implements the Execution Boundary through hook protocols evaluated by `ShieldPipeline`.

**Hook protocols** (`veronica_core/shield/hooks.py`):

- `PreDispatchHook`: evaluated before every LLM call dispatch
- `EgressBoundaryHook`: evaluated when a call is about to exit the local runtime to the provider
- `RetryBoundaryHook`: evaluated before each retry attempt
- `BudgetBoundaryHook`: evaluated against current budget state before a call proceeds

**ShieldPipeline** (`veronica_core/shield/pipeline.py`) evaluates registered hooks in order and returns a `Decision`.

**Decision enum** (`veronica_core/shield/types.py`):

| Decision    | Meaning                                              |
|-------------|------------------------------------------------------|
| ALLOW       | Call proceeds normally                               |
| RETRY       | Retry with modified parameters (e.g., after backoff) |
| HALT        | Hard stop; raise exception to caller                 |
| DEGRADE     | Allow but at reduced capability level                |
| QUARANTINE  | Isolate the agent run; no further calls permitted    |
| QUEUE       | Defer the call to the scheduler                      |

**Default behavior**: all hooks return `None` (no opinion). The pipeline returns `ALLOW`.
This means adding VERONICA to an existing agent is non-breaking and opt-in by default.

---

## Where SafeMode Fits

`SafeModeHook` (`veronica_core/shield/safe_mode.py`) implements both `PreDispatchHook` and `RetryBoundaryHook`.

- **Enabled**: blocks tool dispatch at `PreDispatchHook` and suppresses all retries at `RetryBoundaryHook`
- **Disabled**: returns `None` from both hooks, having no effect on pipeline output
- **Toggle**: manual only via `ShieldConfig(safe_mode=SafeModeConfig(enabled=True))`; no automatic escalation is implemented yet

SafeMode is opt-in and disabled by default.
Enabling it does not change the hook registration; it changes what the already-registered hook returns.
