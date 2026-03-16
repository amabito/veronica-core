---
title: "feat: authority-aware execution policy"
labels: enhancement, policy, security
---

## Why

The current policy engine evaluates rules based on command, path, and URL patterns. It does not consider who is requesting the action. An untrusted agent should not have the same execution rights as a privileged orchestrator.

In multi-agent systems, different callers have different trust levels. A sub-agent spawned by an untrusted user input should not be able to execute shell commands that a system orchestrator is allowed to run. Flat policy is insufficient for this.

## Goal

Add caller identity and trust level to PolicyContext. Policy rules can gate on authority level, so the same command is allowed for an orchestrator and denied for an untrusted agent.

## Scope

- `AuthorityLevel` enum: `UNTRUSTED`, `USER`, `ORCHESTRATOR`, `SYSTEM`
- `authority` field on `PolicyContext` (defaults to `UNTRUSTED` for fail-closed)
- Rule conditions that check `authority >= required_level`
- At least 2 built-in rules that gate on authority

## Non-goals

- Authentication (verifying the caller is who they claim to be)
- Identity management (user databases, tokens)
- Federation or cross-process authority propagation

## Why now

Multi-agent deployments are the primary growth area for VERONICA-Core. The current flat policy model becomes a liability as systems grow. Authority-aware policy is the minimal mechanism needed before the `v4.0` federation roadmap.

## Acceptance criteria

- [ ] `AuthorityLevel` enum with 4 levels (UNTRUSTED < USER < ORCHESTRATOR < SYSTEM)
- [ ] `PolicyContext.authority: AuthorityLevel` field
- [ ] `PolicyContext` defaults to `UNTRUSTED` when authority is not specified
- [ ] At least 2 rules that gate on authority (e.g. shell execution requires ORCHESTRATOR)
- [ ] Tests: authority level comparisons, rule gating at each level, default behavior
- [ ] No breaking changes to existing PolicyContext consumers
