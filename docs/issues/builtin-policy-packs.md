---
title: "feat: expand built-in policy packs"
labels: enhancement, policy, dx
---

## Why

The current `veronica_core/policies/` directory has one policy: `MinimalResponsePolicy`. Users need ready-to-use policy presets that cover common deployment scenarios. Without them, every user must write the same boilerplate to get basic containment in place.

The goal is: `pip install veronica-core`, pick a policy, done.

## Goal

Add 5 policy presets covering the most common containment scenarios.

## Policies

| Name | What it does |
|------|-------------|
| `ReadOnlyAssistantPolicy` | Denies shell commands, file writes, and non-GET network requests |
| `NoNetworkPolicy` | Denies all outbound network access |
| `NoShellPolicy` | Denies all shell and subprocess execution |
| `ApproveSideEffectsPolicy` | Requires approval for write/network/destructive; auto-approves reads |
| `UntrustedToolModePolicy` | Strictest sandbox: denies shell, network, file write; allows reads and LLM calls only |

## Scope

- 5 policy files in `src/veronica_core/policies/`
- Each policy is a standalone dataclass with `enabled` flag and `create_event()` method
- Tests for each: at least one allowed operation and one denied operation
- `__all__` updated in `policies/__init__.py`

## Non-goals

- Custom policy builder UI
- Policy marketplace or registry
- YAML/JSON configuration for these presets (separate: policy loader already handles that)

## Why now

Lowers adoption barrier. "Getting started" for new users currently requires reading the internals to understand what to configure. Presets make the answer obvious.

## Acceptance criteria

- [ ] 5 policy files following `MinimalResponsePolicy` patterns
- [ ] Each policy has Apache 2.0 license header
- [ ] Each policy has module-level docstring with usage example
- [ ] `policies/__init__.py` exports all 5
- [ ] `tests/test_builtin_policies.py` with allowed/denied tests for each
- [ ] `uv run ruff check` passes on new files
- [ ] `uv run pytest tests/test_builtin_policies.py` passes
