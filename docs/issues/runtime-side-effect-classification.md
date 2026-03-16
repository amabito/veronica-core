---
title: "feat: runtime side-effect classification"
labels: enhancement, policy, approval
---

## Why

Tools have different side-effect profiles: some only read data, others write files, make network requests, or execute financial transactions. The current policy engine treats all tool calls equally. This makes it impossible to build approval workflows that auto-approve safe operations while requiring human confirmation for destructive ones.

## Goal

Classify tool calls by side-effect type. Allow policies to gate on side-effect class. Provide a standard annotation mechanism so tool authors can declare what a tool does.

## Scope

- `SideEffectClass` enum: `READ_ONLY`, `LOCAL_WRITE`, `NETWORK`, `FINANCIAL`, `DESTRUCTIVE`
- Tool annotation: a decorator or metadata field that declares the class
- `ToolCallContext.side_effect_class` field (populated at dispatch time)
- ShieldPipeline integration: evaluate rules that gate on side-effect class
- Built-in rule: require approval for FINANCIAL and DESTRUCTIVE

## Non-goals

- Automatic runtime detection of side effects (static annotation only)
- Runtime taint tracking
- Enforcement of declared side effects (a tool that lies about its class is out of scope)

## Why now

Approval workflows are the most-requested feature from early adopters. The side-effect classification is the prerequisite: without it, approval policies cannot distinguish "safe to auto-approve" from "requires review".

## Acceptance criteria

- [ ] `SideEffectClass` enum with 5 values
- [ ] Annotation mechanism (decorator or metadata dict field)
- [ ] `ToolCallContext.side_effect_class` populated at dispatch
- [ ] ShieldPipeline rule condition `side_effect_class in [...]`
- [ ] Built-in rule: FINANCIAL and DESTRUCTIVE require approval
- [ ] Tests: annotation, dispatch wiring, rule gating, unannotated tools default to READ_ONLY
