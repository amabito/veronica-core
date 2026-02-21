# XFAILED Test Registry

Tests marked `xfail` represent known limitations in the current implementation.
Each entry is assessed for safety risk before being accepted as a long-term xfail.

## Risk levels: None / Low / High

| Test | Reason | Safety Risk | Action |
|------|--------|-------------|--------|
| `test_openclaw_api_detection.py::test_run_api_detection` | OpenClaw multi-API detection not yet implemented (v0.2.x). Only `.execute()` is currently supported; `.run()` silently falls back to SAFE_MODE instead of dispatching. | None | ACCEPTED |
| `test_openclaw_api_detection.py::test_callable_api_detection` | OpenClaw multi-API detection not yet implemented (v0.2.x). Callable strategies (`__call__`) fall back to SAFE_MODE instead of dispatching. | None | ACCEPTED |
| `test_openclaw_api_detection.py::test_unsupported_api_error` | OpenClaw multi-API detection not yet implemented (v0.2.x). Unsupported APIs do not yet produce a descriptive error message; they fall back to SAFE_MODE. | None | ACCEPTED |
| `test_openclaw_api_detection.py::test_api_priority_run_over_callable` | OpenClaw multi-API detection not yet implemented (v0.2.x). Priority ordering between `.run()` and `__call__` is not yet enforced. | None | ACCEPTED |

## Safety Assessment

All four xfailed tests relate to a **planned feature** (OpenClaw multi-API detection)
and **not to security controls**.

- **Containment layer is unaffected**: Strategies with `.run()` or callable APIs
  do not bypass the PolicyEngine or SAFE_MODE; they fail safely by transitioning
  to SAFE_MODE rather than executing uncontrolled code.
- **No security gap**: The containment layer's fail-closed design means that
  unrecognised API patterns are blocked, not passed through.
- **Resolution timeline**: Multi-API detection is planned for v0.2.x.

## Resolution Plan

When OpenClaw v0.2.x is released and multi-API detection is implemented:

1. Remove `xfail` markers from the four tests above.
2. Implement the feature and confirm all four tests pass.
3. Remove the relevant entries from this registry.

## How to Add a New xfail

Before marking a test `xfail`, the submitter MUST:

1. Document the reason in this registry.
2. Assess the safety risk (None / Low / High).
3. If risk is **High**: fix the underlying issue instead of marking `xfail`.
4. Get the registry entry reviewed in the PR that adds the `xfail`.

No test with **High** safety risk may be accepted as a permanent `xfail`.
