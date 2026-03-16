# Authority-Aware Execution Policy

## What it does

VERONICA-Core distinguishes the origin of a runtime action request
(developer policy, user input, tool output, retrieved content, etc.)
and varies the enforcement decision accordingly.

An `AuthorityClaim` is attached to each `ExecPolicyContext`. The
`PolicyEngine` checks the claim before applying action-specific rules.
Insufficient authority produces a `DENY` or `REQUIRE_APPROVAL` decision
with an audit record.

## Why

A tool output should not have the same execution rights as a developer-set
policy. Retrieved content should not silently trigger shell commands.
Agent-generated intermediate state should not cause unauthorized side effects.
This model makes authority an explicit first-class concept rather than an
implicit assumption baked into each rule.

## Authority sources

| Source | Trust ceiling | Typical use |
|--------|--------------|-------------|
| `developer_policy` | privileged | Hardcoded rules in application code |
| `system_config` | privileged | Environment or config-file settings |
| `user_input` | trusted | Direct user messages |
| `approved_override` | trusted | Human-approved escalation |
| `tool_output` | provisional | LLM tool call results |
| `retrieved_content` | provisional | RAG or search results |
| `memory_content` | provisional | Conversation history, stored context |
| `agent_generated` | provisional | Agent intermediate reasoning |
| `external_message` | untrusted | Cross-agent or external API messages |
| `unknown` | untrusted | No authority specified (fail-closed) |

Trust levels in ascending order: `untrusted` (0), `provisional` (1),
`trusted` (2), `privileged` (3).

## How it works

1. Action request is tagged with an `AuthorityClaim` at creation time.
2. `AuthorityClaim` flows through `ExecutionContext` and `ExecPolicyContext`.
3. `PolicyEngine` checks authority before evaluating action-specific rules.
4. Insufficient authority produces `DENY` or `REQUIRE_APPROVAL`.
5. Authority decisions are recorded in the audit log via `write_authority_event`.

### Trust ceiling enforcement

`effective_trust_level` is the minimum of the source ceiling and any
explicitly asserted trust. A `tool_output` cannot claim `privileged`:

```python
from veronica_core.security.authority import AuthorityClaim, AuthoritySource

claim = AuthorityClaim(
    source=AuthoritySource.TOOL_OUTPUT,
    asserted_trust="privileged",  # capped to "provisional"
)
assert claim.effective_trust_level == "provisional"
```

## Default behavior

If no authority is specified, `UNKNOWN_AUTHORITY` is used (untrusted).
This is fail-closed: untagged actions are treated as the least trusted
possible origin.

```python
from veronica_core.security.authority import UNKNOWN_AUTHORITY

assert UNKNOWN_AUTHORITY.effective_trust_level == "untrusted"
assert UNKNOWN_AUTHORITY.trust_rank == 0
```

## Authority derivation

When one action spawns a child action, use `derives()` to propagate
authority without escalation:

```python
user_claim = AuthorityClaim(source=AuthoritySource.USER_INPUT)
tool_claim = user_claim.derives(AuthoritySource.TOOL_OUTPUT)
# tool_claim.trust_rank <= user_claim.trust_rank  -- no escalation
```

The derivation chain is recorded for audit purposes:

```python
assert "user_input" in tool_claim.chain
```

## Human approval escalation

A provisional claim can be elevated to `APPROVED_OVERRIDE` with an
approval ID for audit traceability:

```python
original = AuthorityClaim(source=AuthoritySource.AGENT_GENERATED)
approved = original.with_approval("approval-001")
assert approved.source is AuthoritySource.APPROVED_OVERRIDE
assert approved.approval_id == "approval-001"
assert approved.effective_trust_level == "trusted"
```

The approval grants up to `trusted` (rank 2). It cannot reach `privileged`.

## Usage with PolicyEngine

```python
from veronica_core.security.authority import AuthorityClaim, AuthoritySource
from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import ExecPolicyContext, PolicyEngine

engine = PolicyEngine()

ctx = ExecPolicyContext(
    action="shell",
    args=["pytest", "--version"],
    working_dir="/repo",
    repo_root="/repo",
    user=None,
    caps=CapabilitySet.dev(),
    env="dev",
    authority=AuthorityClaim(source=AuthoritySource.USER_INPUT),
)

decision = engine.evaluate(ctx)
# decision.verdict in ("ALLOW", "DENY", "REQUIRE_APPROVAL")
```

## Audit logging

Use `AuditLog.write_authority_event` to record authority decisions:

```python
from veronica_core.audit.log import (
    AuditLog,
    AUTHORITY_POLICY_DENIED,
    AUTHORITY_POLICY_ALLOWED,
)
from pathlib import Path

log = AuditLog(Path("/tmp/veronica_audit/authority.jsonl"))

log.write_authority_event(
    event_type=AUTHORITY_POLICY_DENIED,
    authority_source="tool_output",
    effective_trust_level="provisional",
    action="shell",
    decision="DENY",
    reason="authority insufficient: source=tool_output, effective_trust=provisional",
    chain=("user-abc", "agent-1"),
)
```

Available event type constants:

| Constant | Meaning |
|----------|---------|
| `AUTHORITY_ASSIGNED` | Claim attached to an action request |
| `AUTHORITY_PROPAGATED` | Claim propagated via `derives()` |
| `AUTHORITY_OVERRIDE_APPROVED` | Human approval elevated the claim |
| `AUTHORITY_POLICY_DENIED` | Engine denied due to insufficient authority |
| `AUTHORITY_POLICY_DEGRADED` | Engine returned `REQUIRE_APPROVAL` |
| `AUTHORITY_POLICY_ALLOWED` | Engine allowed; authority was sufficient |

## What it does not do

- It does not detect prompt injection (semantic analysis is out of scope).
- It does not verify model output content (content safety is separate).
- It does not manage identity (authentication is external).
- It does not verify that a caller accurately self-reports its `AuthoritySource`.
  Correct tagging is the responsibility of the integration layer.
