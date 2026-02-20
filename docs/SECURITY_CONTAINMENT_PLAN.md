# VERONICA Security Containment Layer — Design & Audit Plan

## 1. Problem Statement

AI agents operating with tool-use capabilities can be **weaponized** or can
**bypass upper-layer controls** in ways their operators did not intend:

- Agents may receive adversarial prompts instructing them to exfiltrate secrets
  (`.env`, SSH keys, cloud credentials) via outbound HTTP.
- Agents may execute uncontrolled shell commands that delete files, modify CI
  pipelines, or install malicious packages.
- Agents may be prompted to push code to remote repositories without human
  review, or to alter GitHub Actions workflows.
- Once an agent is compromised, it can attempt to disable its own safety
  mechanisms unless those mechanisms are enforced at a **lower** layer.

---

## 2. Design Philosophy

### "Cannot do" vs. "Should not do"

Upper-layer controls (system prompts, rules files) tell the agent what it
_should not do_.  They are advisory and can be overridden by a sufficiently
adversarial prompt.

The Security Containment Layer enforces what the agent **cannot do** —
regardless of what instructions it receives — by intercepting every tool
dispatch and egress request at the OS/process boundary.

### Defense in Depth

```
Layer 0 (upper): System prompt / rules / agent persona
Layer 1 (this):  PolicyEngine + Adapter + SecureExecutor  [THIS LAYER]
Layer 2 (OS):    Linux seccomp / cgroups / network namespaces (Phase B+)
```

Even if Layer 0 is fully bypassed, Layer 1 stops the action.
Even if Layer 1 is degraded, Layer 2 (sandbox) provides a hard boundary.

### Fail-Closed

All evaluations default to `DENY`.  An action must be **explicitly allowed**
by a rule to proceed.  Unknown actions are always denied.

---

## 3. Architecture

```
Agent / LLM tool call
        |
        v
  PolicyHook.before_tool_call(ToolCallContext)
        |
        v
  PolicyEngine.evaluate(PolicyContext)
        |
        +---> DENY  -----------> Decision.HALT  (blocked, logged)
        |
        +---> REQUIRE_APPROVAL -> Decision.QUARANTINE (queued for human)
        |
        +---> ALLOW -----------> Adapter.exec(args)
                                        |
                                        v
                               SecureExecutor (Phase B)
                                        |
                                        v
                               subprocess (shell=False, cwd=repo_root)
                                        |
                                        v
                               Tamper-evident audit log (Phase C)
```

**Egress path (outbound HTTP):**

```
Agent HTTP call
        |
        v
  PolicyHook.before_egress(ToolCallContext, url, method)
        |
        v
  PolicyEngine.evaluate(PolicyContext action="net")
        |
        +---> DENY  -----------> Decision.HALT  (connection blocked)
        +---> ALLOW -----------> HTTP request proceeds
```

---

## 4. Capability Model

Capabilities gate which operations an agent profile may perform.
All capabilities are evaluated in addition to policy rules.

| Capability              | dev | ci  | audit |
|-------------------------|-----|-----|-------|
| READ_REPO               | yes | yes | yes   |
| EDIT_REPO               | yes | no  | no    |
| BUILD                   | yes | yes | no    |
| TEST                    | yes | yes | no    |
| NET_FETCH_ALLOWLIST     | no  | no  | no    |
| GIT_PUSH_APPROVAL       | no  | no  | no    |
| SHELL_BASIC             | yes | no  | no    |
| FILE_READ_SENSITIVE     | no  | no  | no    |

`GIT_PUSH_APPROVAL` must be explicitly granted; it is not included in any
built-in profile.  `NET_FETCH_ALLOWLIST` controls whether allowlisted GET
requests are permitted (default: denied unless explicitly set).

---

## 5. Policy Engine Decision Flow

```
evaluate(ctx: PolicyContext) -> PolicyDecision
    |
    +-- action == "shell"?
    |       argv[0] in DENY_COMMANDS?       -> DENY  (risk +8)
    |       any DENY_OPERATOR in args?      -> DENY  (risk +6)
    |       metadata.file_count > 20?       -> REQUIRE_APPROVAL (risk +3)
    |       argv[0] in ALLOW_COMMANDS?      -> ALLOW (risk 0)
    |       else                            -> DENY  (risk +5)
    |
    +-- action == "file_read"?
    |       path matches DENY_PATTERNS?     -> DENY  (risk +7)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "file_write"?
    |       path matches APPROVAL_PATTERNS? -> REQUIRE_APPROVAL (risk +4)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "net"?
    |       method in DENY_METHODS?         -> DENY  (risk +6)
    |       host not in GET_ALLOWLIST?      -> DENY  (risk +5)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "git"?
    |       subcmd in DENY_SUBCMDS
    |         AND NOT has_cap GIT_PUSH_APPROVAL? -> DENY (risk +7)
    |       else                            -> ALLOW (risk 0)
    |
    +-- action == "browser"?                -> DENY  (risk +5)
    |
    +-- unknown action                      -> DENY  (risk +5)
```

---

## 6. Integration: PolicyHook into ShieldPipeline

```python
from veronica_core.security import PolicyEngine, PolicyHook, CapabilitySet
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.config import ShieldConfig

# Build the hook
engine = PolicyEngine()                    # uses built-in rules
hook = PolicyHook(
    engine=engine,
    caps=CapabilitySet.ci(),               # restrict to CI profile
    working_dir="/repo",
    repo_root="/repo",
    env="ci",
)

# Wire into the pipeline
config = ShieldConfig()
pipeline = ShieldPipeline(
    config=config,
    tool_dispatch=[hook],                  # PolicyHook as ToolDispatchHook
    egress=[hook],                         # PolicyHook as EgressBoundaryHook
)
```

The `PolicyHook.last_decision` attribute exposes the most recent
`PolicyDecision` for inspection (e.g. to read `risk_score_delta`).

---

## 7. Audit Trail and SAFE_MODE Auto-Transition

Each `PolicyDecision` carries a `risk_score_delta` (0–10).

**Phase D** (risk accumulation) aggregates these deltas in a sliding window.
When the cumulative score exceeds a configured threshold, the system
automatically transitions to `SAFE_MODE` — blocking all further tool dispatch
until an operator resets the state.

```
risk_score_delta accumulation (sliding window):
  +0   safe action (ALLOW)
  +3   suspicious (REQUIRE_APPROVAL: large file change)
  +4   sensitive write (REQUIRE_APPROVAL: workflow/script)
  +5   unknown host or unrecognised command (DENY)
  +6   pipe operator or mutating HTTP method (DENY)
  +7   sensitive file read or git push without cap (DENY)
  +8   dangerous shell command e.g. rm, powershell (DENY)

Threshold example: 30 points in 60 seconds -> transition to SAFE_MODE
```

This means repeated probing attacks (an agent trying many blocked actions)
will rapidly accumulate risk score and trigger SAFE_MODE, even if no single
action is catastrophic.

---

## 8. Critical Audit Findings Coverage

| Finding   | Attack Vector                              | Blocked by                                              |
|-----------|--------------------------------------------|---------------------------------------------------------|
| CRITICAL-1 | Uncontrolled shell execution (shell=True) | Adapter (Phase A-3): `shell=False` + argv allowlist     |
|            |                                            | PolicyEngine: SHELL_DENY_CMD, SHELL_DENY_OPERATOR       |
| CRITICAL-2 | .env / secret file read                   | PolicyEngine: FILE_READ_DENY_SENSITIVE                  |
|            |                                            | SecretMasker (A-4): redacts any leaked secrets in output|
| CRITICAL-3 | Unauthenticated outbound POST/PUT/DELETE   | PolicyEngine: NET_DENY_METHOD                           |
|            |                                            | PolicyHook.before_egress: maps DENY -> Decision.HALT    |
| CRITICAL-4 | CI/workflow file modification              | PolicyEngine: FILE_WRITE_REQUIRE_APPROVAL               |
|            |                                            | Returns QUARANTINE, queued for human approval           |
| CRITICAL-5 | Runaway agent SAFE_MODE bypass             | Phase D: risk_score_delta accumulation -> SAFE_MODE     |
|            |                                            | SAFE_MODE blocks ALL tool dispatch unconditionally      |

**Key principle:** Even if the upper environment (system prompt, agent rules)
is fully bypassed by an adversarial prompt, the lower containment layer
intercepts and blocks the action.  The agent literally cannot execute the
operation — not merely should not.

---

## 9. Custom Policy Override

To customize rules, edit `policies/default.yaml`:

```yaml
# policies/custom.yaml
version: "1.0"

deny:
  shell:
    commands:
      - rm
      - del
      # add your own blocked commands here

allow:
  shell:
    commands:
      - pytest
      - python
      # add your own allowed commands here
```

Then load the custom policy:

```python
from pathlib import Path
from veronica_core.security import PolicyEngine

engine = PolicyEngine(policy_path=Path("policies/custom.yaml"))
```

The YAML file is currently used as documentation; future versions will parse
it into live rule objects.  For now, fork `_EVALUATORS` in `policy_engine.py`
to add custom evaluator functions.

---

## 10. Defense-in-Depth Guarantee

> "Even if the upper environment is bypassed, the lower layer stops it."

The Security Containment Layer is designed so that:

1. **PolicyEngine** enforces rules independently of agent instructions.
2. **Adapter** (Phase A-3) always uses `shell=False`, never interpolating
   arguments into a shell string — preventing injection via argv.
3. **SecureExecutor** (Phase B) runs inside a sandboxed process with reduced
   OS-level privileges (seccomp, cgroups).
4. **Tamper-evident log** (Phase C) records every decision with a chained
   HMAC, ensuring audit records cannot be silently altered by a compromised
   agent.
5. **Risk accumulation** (Phase D) triggers SAFE_MODE on repeated suspicious
   activity, stopping the agent even if each individual action was borderline.

No single layer is sufficient on its own.  The layers are deliberately
redundant so that compromising any one layer does not grant the agent
unrestricted capability.
