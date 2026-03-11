# Kernel Contract

veronica-core is the enforcement kernel. It is deterministic, fail-closed,
and policy-attested. It does not schedule agents, manage tenants, or provide
a UI. Those responsibilities belong to the control plane
([veronica](https://github.com/amabito/veronica-public)).

---

## Boundary

```
veronica-core (kernel)          veronica (control plane)
-----------------------------   ----------------------------
Budget enforcement              Policy management UI
Step/retry/timeout limits       Fleet coordination
Circuit breaker (local+Redis)   Dashboard / alerting
Execution graph                 Tenant isolation
PolicyEngine + AuditLog         Rollout / canary
DecisionEnvelope attestation    Federation (future)
MCP/framework adapters          Replay / simulation
Security containment            User authentication
```

The kernel exposes hooks and types. The control plane consumes them.
The kernel never imports from the control plane.

---

## Kernel Hooks

Defined in `shield/hooks.py` and `containment/execution_context.py`:

| Hook | Location | Purpose |
|------|----------|---------|
| `PreDispatchHook.before_llm_call` | shield/hooks.py | Gate before LLM API call |
| `PostDispatchHook.after_llm_call` | shield/hooks.py | Inspect LLM response |
| `ToolDispatchHook.before_tool_call` | shield/hooks.py | Gate before tool invocation |
| `EgressBoundaryHook.before_egress` | shield/hooks.py | Gate before HTTP request |
| `RetryBoundaryHook.on_error` | shield/hooks.py | Intercept exceptions |
| `BudgetBoundaryHook.before_charge` | shield/hooks.py | Cost tracking |
| `ExecutionContext.record_event` | execution_context.py | Append audit event |

Hooks return a `Decision` (ALLOW, HALT, DEGRADE, QUARANTINE, RETRY, QUEUE).
A HALT blocks the call. The kernel enforces that evaluation occurs before
the call proceeds.

---

## Decision Contract

### DecisionEnvelope (kernel/decision.py)

Attestation wrapper for governance decisions. Frozen dataclass.

| Field | Type | Description |
|-------|------|-------------|
| `decision` | str | ALLOW, DENY, HALT, DEGRADE, QUARANTINE, RETRY, QUEUE |
| `policy_hash` | str | SHA-256 of active PolicyBundle (empty if none) |
| `reason_code` | str | Machine-readable code from ReasonCode enum |
| `reason` | str | Human-readable explanation |
| `audit_id` | str | UUID4, unique per decision |
| `timestamp` | float | Unix epoch from time.time() |
| `policy_epoch` | int | Monotonic counter from PolicyMetadata |
| `issuer` | str | Component name (e.g. "BudgetEnforcer") |
| `metadata` | dict | Arbitrary key/value, frozen via MappingProxyType |

**Status**: Optional. Not all decision paths attach an envelope.
Production wiring started on the budget denial path (v3.5.0).
Other paths emit decisions without envelopes.

### Decision vocabulary

| Value | allowed | denied | Semantics |
|-------|---------|--------|-----------|
| ALLOW | yes | no | Proceed normally |
| DEGRADE | yes | no | Proceed with reduced capability |
| QUARANTINE | yes | no | Proceed, marked for review |
| RETRY | yes | no | Proceed, suggests retry |
| QUEUE | yes | no | Proceed, suggests queueing |
| DENY | no | yes | Operation rejected, agent continues |
| HALT | no | yes | Operation rejected, agent run terminates |

DENY and HALT are both "denied" but differ in scope:
- DENY: this operation is blocked; the agent may try something else.
- HALT: this operation is blocked; the entire agent run must stop.

### PolicyDecision (runtime_policy.py)

Result of a RuntimePolicy.check() evaluation. Carries an optional
`envelope: DecisionEnvelope` field for attestation.

The `allowed: bool` field is the primary gate. When `envelope` is present,
`envelope.decision` provides the categorical outcome. When absent, only
`allowed` and `reason` are available.

### Domain-specific vocabularies

Memory governance uses `GovernanceVerdict` (ALLOW, DENY, QUARANTINE, DEGRADE).
Security policy uses `ExecPolicyDecision.verdict` (ALLOW, DENY, REQUIRE_APPROVAL).
These are domain-specific subsets of the kernel vocabulary.

---

## Signed Audit Events

### Current state

- **Hash chain**: SHA-256 chain linking each audit entry to its predecessor.
  Proves internal consistency (no entry deleted or reordered). Does NOT prove
  authenticity -- an adversary with write access can recompute valid hashes.

- **HMAC-SHA256 signing**: Optional per-entry HMAC via duck-typed `signer`
  with `sign_bytes()` method. When enabled, each entry carries an `hmac` field.
  Chain verification (`verify_chain()`) checks HMAC on every entry when a
  signer is provided.

- **Terminology**: "tamper-evident internal audit trail" -- not "tamper-proof",
  not "publicly verifiable". HMAC is symmetric-key; it proves the signer
  produced the entry, but a third party cannot verify without the key.

- **Ed25519 signing**: `PolicySignerV2` supports Ed25519 for policy bundle
  signatures (requires `cryptography` package). This is policy-level, not
  per-audit-entry.

---

## HA ABI

### Current state

Defined in `kernel/ha.py`. Types only -- no business logic.

| Type | Purpose | Status |
|------|---------|--------|
| `ReservationState` | Enum: PENDING, COMMITTED, ROLLED_BACK, EXPIRED | Defined |
| `Reservation` | Immutable receive slot with lifecycle | Defined |
| `HeartbeatSnapshot` | Point-in-time kernel liveness snapshot | Defined |
| `BreakerReflection` | Read-only circuit breaker state | Defined |

These are **ABI surface types** for future HA integration. No consensus,
no federation, no peer governance is implemented. Authoritative state is
always external -- these types define the shape of data the kernel can
emit or receive.

The module docstring states: "Authoritative state is ALWAYS external.
This module defines the minimum surface needed so that future HA
components can observe kernel state."

---

## Sample Audit Payload

Budget denial on the `BudgetEnforcer.check()` path produces:

```python
from veronica_core.budget import BudgetEnforcer
from veronica_core.runtime_policy import PolicyContext

enforcer = BudgetEnforcer(limit_usd=10.0)
enforcer.spend(9.50)

decision = enforcer.check(PolicyContext(cost_usd=2.00))
# decision.allowed == False
# decision.envelope is not None (budget denial path is wired)
```

The `decision.envelope` contains:

```json
{
  "decision": "DENY",
  "policy_hash": "",
  "reason_code": "BUDGET_EXCEEDED",
  "reason": "Budget would exceed: $11.50 > $10.00",
  "audit_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "timestamp": 1741689600.123,
  "policy_epoch": 0,
  "issuer": "BudgetEnforcer"
}
```

On paths where no envelope is wired (e.g. step limit ALLOW), the
`decision.envelope` field is `None`:

```python
decision = enforcer.check(PolicyContext(cost_usd=0.50))
# decision.allowed == True
# decision.envelope is None
```

Envelope attachment is opt-in per decision path. Not all paths produce
envelopes. The `envelope` field on `PolicyDecision` is
`Optional[DecisionEnvelope]` and defaults to `None`.
