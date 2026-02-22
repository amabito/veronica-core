# Migration Guide: veronica-core v0.10.2 → v0.10.3

## What changed

`"make"` has been removed from `SHELL_ALLOW_COMMANDS`. Any call to
`SecureExecutor.execute_shell(["make", ...])` now raises `SecurePermissionError`
with `rule_id="SHELL_DENY_CMD"`.

Policy YAML parse failure is now fail-closed: if the policy file exists but cannot
be parsed, `PolicyEngine._load_policy()` raises `RuntimeError` instead of returning
`{}` silently.

---

## Why make was removed

`make` executes recipe lines by spawning subshells. Those subshells run arbitrary
commands that PolicyEngine cannot observe. PolicyEngine inspects the argv at the
point of call only. Allowlisting `make` at the argv level does not contain what
`make` subsequently invokes; it provides a false sense of containment.

veronica-core enforces execution policy at the argv boundary. It is not an OS-level
sandbox. Build tools that manage their own execution environment cannot be safely
proxied by an argv-inspection layer.

---

## Option 1: Replace with direct tool calls (recommended)

Replace each `make` target with the commands it invokes, so each action is
individually evaluated by PolicyEngine.

```python
# Before
executor.execute_shell(["make", "test"])

# After
executor.execute_shell(["pytest", "tests/", "-q"])
```

```python
# Before
executor.execute_shell(["make", "build"])

# After: invoke the compiler directly
executor.execute_shell(["gcc", "-O2", "-o", "build/app", "src/main.c"])
```

This is the preferred approach: every sub-command becomes a first-class action
visible to PolicyEngine.

---

## Option 2: Move build steps outside the policy boundary

Build steps that are deterministic infrastructure tasks — not LLM-driven actions —
do not belong inside the veronica-core policy boundary. Run them at the application
or CI level:

- **CI pipelines**: GitHub Actions, GitLab CI, or equivalent run `make` before or
  after the agent session
- **Application code**: call `subprocess.run(["make", ...])` directly in host code
  that does not go through `SecureExecutor`

This is appropriate when the build step is operator-controlled and not part of the
agent's action space.

---

## Option 3: Override the policy (use only with explicit risk acceptance)

If `make` must remain accessible through the policy layer, subclass `PolicyEngine`
and re-add it locally. The override documents that the caller accepts the structural
limitation:

```python
from veronica_core.security.policy_engine import PolicyEngine, PolicyDecision, PolicyContext

class BuildAllowedPolicy(PolicyEngine):
    """
    Re-allows make for controlled build environments.

    WARNING: make spawns subshells for recipe execution. Those subshells operate
    outside PolicyEngine's observation. This override accepts that risk explicitly.
    Use only when the Makefile is fully operator-controlled and the agent cannot
    influence Makefile content or the -f argument. External process isolation
    (container, VM, or equivalent) is strongly recommended.
    """

    def _eval_shell(self, ctx: PolicyContext) -> PolicyDecision:
        if ctx.args and ctx.args[0] == "make":
            # Reject any flag that could specify an external Makefile.
            # An agent that can influence -f can still escape containment.
            for token in ctx.args[1:]:
                if token == "-f" or token.startswith("-f"):
                    break
            else:
                return PolicyDecision(
                    verdict="ALLOW",
                    rule_id="LOCAL_MAKE_OVERRIDE",
                    reason="make allowlisted by local policy (sub-shell risk accepted)",
                )
        return super()._eval_shell(ctx)
```

Before using this option, confirm:

- The Makefile content is operator-controlled, not agent-influenced
- The agent cannot pass a `-f` argument pointing to an external file
- Independent process-level isolation wraps the agent (container, VM, or similar)

---

## Policy YAML migration

If you ship a YAML policy file alongside your deployment, verify it parses cleanly
before upgrading:

```python
from pathlib import Path
from veronica_core.security.policy_engine import PolicyEngine

# Raises RuntimeError if the file exists but cannot be parsed.
# Returns {} if the file does not exist.
result = PolicyEngine._load_policy(Path("policy.yaml"))
print("Policy loaded:", result)
```

Run this check in your CI pipeline before deploying v0.10.3 to confirm no parse
errors exist in your policy file.

---

## Decision table

| Scenario | Recommended path |
|----------|-----------------|
| Agent runs tests | Replace `make test` with `pytest` directly |
| Agent compiles code | Replace `make build` with direct compiler call |
| Build is CI infrastructure | Move outside `SecureExecutor` entirely |
| Makefile is fully operator-controlled, agent cannot influence `-f` | Option 3 with documented risk acceptance |
| Makefile content or `-f` is agent-influenced | No safe option; redesign the tool boundary |
