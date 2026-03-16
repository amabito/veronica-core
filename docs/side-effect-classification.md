# Side-Effect Classification

## What it does

`veronica_core.security.side_effects` classifies actions by what they do at
runtime, not just what they are called.  A shell command that reads a file has
a different risk profile than one that pushes to a remote repository.  This
module provides the vocabulary for those distinctions.

Each action is mapped to a `SideEffectProfile` -- an immutable set of
`SideEffectClass` values describing the potential effects the action has on
local state, remote state, or other agents.

## Side-effect classes

| Class | Severity | Meaning |
|-------|----------|---------|
| `none` | 0 | No observable effect |
| `informational` | 1 | Observes state without reading data (e.g. listing directory names) |
| `read_local` | 2 | Reads local filesystem or process state |
| `write_local` | 4 | Modifies local filesystem or in-process state |
| `cross_agent` | 5 | Communicates with another agent in the same system |
| `shell_execute` | 6 | Spawns a subprocess or executes arbitrary code |
| `outbound_network` | 6 | Makes an outbound network connection |
| `external_mutation` | 8 | Mutates state outside the local system (remote repo, database) |
| `credential_access` | 8 | Reads or uses credentials, secrets, or private keys |
| `irreversible` | 10 | Effect cannot be undone (delete, overwrite, publish) |

Severity is used by `SideEffectProfile.max_severity` and the `has_dangerous`
property (threshold: severity >= 6).

## How classify_action works

`classify_action(action, metadata=None)` looks up the action string in
`ACTION_SIDE_EFFECTS`, a module-level `MappingProxyType` mapping known action
literals to pre-built profiles.

Known action literals: `file_read`, `file_write`, `shell`, `net_request`,
`browser_navigate`, `git_push`, `git_commit`.

For any action not in the mapping, `classify_action` returns a fail-closed
profile with:
- `effects = frozenset()` (no known effects)
- `strict_mode = True` (caller should treat as requiring approval)
- `description` containing the original action string

This means unknown actions are not silently allowed -- the caller can inspect
`profile.strict_mode` and escalate accordingly.

## SideEffectProfile properties

| Property | Type | Meaning |
|----------|------|---------|
| `effects` | `frozenset[SideEffectClass]` | All effects for this action |
| `max_severity` | `int` | Highest severity among all effects; 0 if empty |
| `has_write` | `bool` | True if `write_local` or `shell_execute` is present |
| `has_external` | `bool` | True if `outbound_network`, `external_mutation`, or `cross_agent` is present |
| `has_dangerous` | `bool` | True if `max_severity >= 6` |
| `is_read_only` | `bool` | True if all effects have severity <= 2 (or empty) |
| `audit_summary` | `str` | Comma-separated sorted effect names for log entries |
| `strict_mode` | `bool` | True for unknown-action profiles; signals caller to escalate |

All properties are computed from `effects` with no mutation.
`SideEffectProfile` is a frozen dataclass; the `metadata` dict is wrapped in
`MappingProxyType` at construction.

## Strict and permissive modes

`strict_mode=True` on a profile is a signal, not enforcement.  The
`classify_action` function sets it automatically for unknown actions.
Callers that care about strict vs. permissive handling inspect this flag:

```python
profile = classify_action("some_new_action")
if profile.strict_mode and profile.max_severity >= 6:
    # require approval before proceeding
    ...
```

Built-in policies (`NoShellPolicy`, `NoNetworkPolicy`, etc.) do not inspect
`strict_mode` directly -- they enforce based on action type and command name.
`strict_mode` is intended for higher-level orchestration logic.

## Usage

```python
from veronica_core.security.side_effects import classify_action, SideEffectClass

profile = classify_action("git_push")
assert profile.has_external          # outbound_network + external_mutation
assert profile.has_dangerous         # max severity 8
assert not profile.is_read_only

profile = classify_action("file_read")
assert profile.is_read_only
assert not profile.has_write
```

Building a custom profile for a compound action:

```python
from veronica_core.security.side_effects import SideEffectClass, SideEffectProfile

profile = SideEffectProfile(
    effects=frozenset({
        SideEffectClass.READ_LOCAL,
        SideEffectClass.OUTBOUND_NETWORK,
    }),
    description="read file, then POST to webhook",
)
assert profile.has_external
assert profile.has_dangerous
```

## Audit integration

`veronica_core.audit.log` exports constants for side-effect audit events:

| Constant | Event type string | When emitted |
|----------|------------------|--------------|
| `SIDE_EFFECT_CLASSIFIED` | `side_effect_classified` | Action classified successfully |
| `SIDE_EFFECT_UNKNOWN` | `side_effect_unknown` | classify_action returned strict-mode unknown profile |
| `SIDE_EFFECT_POLICY_ALLOWED` | `side_effect_policy_allowed` | Policy allowed action with known side effects |
| `SIDE_EFFECT_POLICY_DENIED` | `side_effect_policy_denied` | Policy denied due to side effects |
| `SIDE_EFFECT_APPROVAL_REQUIRED` | `side_effect_approval_required` | Side effects require approval before proceeding |

Use `AuditLog.write(SIDE_EFFECT_POLICY_DENIED, {...})` to record decisions.

## What it does not do

- It does not enforce decisions itself.  `SideEffectProfile` is data; policy
  classes decide what to do with it.
- It does not detect what an action actually did at runtime.  It describes what
  the action type is expected to do based on its category.
- It does not classify sub-operations within a shell command (e.g. it does not
  parse `rm -rf` vs `ls -la` -- that is the job of `NoShellPolicy` and the
  shell evaluator in `policy_rules.py`).
- It does not replace authority-based gating.  Side-effect classification and
  authority checks are independent; both may apply to the same action.
