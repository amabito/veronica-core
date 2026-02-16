# OpenClaw Integration Patch Guide

Proposed changes to OpenClaw codebase to add VERONICA safety layer integration as an **optional** feature.

---

## Design Principles

1. **Non-breaking**: Existing OpenClaw users unaffected (integration is opt-in)
2. **Minimal changes**: Add ~50 LOC to OpenClaw codebase
3. **Optional dependency**: VERONICA listed as optional dependency (not required)
4. **Pluggable**: Users can enable/disable safety layer at runtime

---

## Proposed Changes

### Change 1: Add Optional Dependency

**File**: `setup.py` or `pyproject.toml`

**Before**:
```python
# setup.py
setup(
    name="openclaw",
    version="1.0.0",
    install_requires=[
        "numpy>=1.20",
        "pandas>=1.3",
        # ... other dependencies
    ],
)
```

**After**:
```python
# setup.py
setup(
    name="openclaw",
    version="1.0.0",
    install_requires=[
        "numpy>=1.20",
        "pandas>=1.3",
        # ... other dependencies
    ],
    extras_require={
        "safety": ["veronica-core>=0.1.0"],  # ← ADD THIS
    },
)
```

**Install with safety**:
```bash
pip install openclaw[safety]
```

---

### Change 2: Add SafetyAdapter Interface

**File**: `openclaw/safety.py` (new file)

**Content**:
```python
"""Optional safety layer integration for OpenClaw.

Requires: pip install openclaw[safety]
"""

from typing import Any, Dict, Optional, Protocol


class SafetyLayer(Protocol):
    """Protocol for safety layer integration (e.g., VERONICA)."""

    def is_in_cooldown(self, entity_id: str) -> bool:
        """Check if entity is in cooldown."""
        ...

    def record_pass(self, entity_id: str) -> None:
        """Record successful execution."""
        ...

    def record_fail(self, entity_id: str) -> bool:
        """Record failed execution. Returns True if cooldown activated."""
        ...


class NoOpSafetyLayer:
    """No-op safety layer (default, no overhead)."""

    def is_in_cooldown(self, entity_id: str) -> bool:
        return False

    def record_pass(self, entity_id: str) -> None:
        pass

    def record_fail(self, entity_id: str) -> bool:
        return False


def create_veronica_safety_layer(
    cooldown_fails: int = 3,
    cooldown_seconds: int = 600,
) -> SafetyLayer:
    """Create VERONICA safety layer (if installed).

    Args:
        cooldown_fails: Circuit breaker threshold
        cooldown_seconds: Cooldown duration

    Returns:
        SafetyLayer instance

    Raises:
        ImportError: If veronica-core not installed
    """
    try:
        from veronica_core import VeronicaIntegration

        class VeronicaSafetyAdapter:
            """Adapts VERONICA to SafetyLayer protocol."""

            def __init__(self, cooldown_fails: int, cooldown_seconds: int):
                self.veronica = VeronicaIntegration(
                    cooldown_fails=cooldown_fails,
                    cooldown_seconds=cooldown_seconds,
                )
                self.entity_id = "openclaw_strategy"

            def is_in_cooldown(self, entity_id: str = None) -> bool:
                eid = entity_id or self.entity_id
                return self.veronica.is_in_cooldown(eid)

            def record_pass(self, entity_id: str = None) -> None:
                eid = entity_id or self.entity_id
                self.veronica.record_pass(eid)

            def record_fail(self, entity_id: str = None) -> bool:
                eid = entity_id or self.entity_id
                return self.veronica.record_fail(eid)

        return VeronicaSafetyAdapter(cooldown_fails, cooldown_seconds)

    except ImportError:
        raise ImportError(
            "veronica-core not installed. "
            "Install with: pip install openclaw[safety]"
        )
```

**Lines added**: ~70

---

### Change 3: Integrate Safety Layer in Strategy Class

**File**: `openclaw/strategy.py` (or main strategy module)

**Before**:
```python
class Strategy:
    def __init__(self, config: dict):
        self.config = config
        # ... initialization

    def execute(self, context: dict) -> dict:
        # ... strategy logic
        result = self._compute_signal(context)
        return result
```

**After**:
```python
class Strategy:
    def __init__(self, config: dict, safety_layer: Optional[SafetyLayer] = None):
        self.config = config
        self.safety_layer = safety_layer or NoOpSafetyLayer()  # ← ADD THIS
        # ... initialization

    def execute(self, context: dict) -> dict:
        # ← ADD SAFETY CHECK (3 lines)
        if self.safety_layer.is_in_cooldown("strategy"):
            return {"status": "blocked", "reason": "Circuit breaker active"}

        try:
            # ... strategy logic
            result = self._compute_signal(context)

            # ← ADD SUCCESS RECORDING (1 line)
            self.safety_layer.record_pass("strategy")

            return result
        except Exception as e:
            # ← ADD FAILURE RECORDING (1 line)
            self.safety_layer.record_fail("strategy")
            raise
```

**Lines added**: ~5

---

### Change 4: Update Documentation

**File**: `README.md`

**Add section**:
```markdown
## Optional Safety Layer (VERONICA Integration)

OpenClaw can optionally integrate with VERONICA for production-grade execution safety:

### Installation
```bash
pip install openclaw[safety]
```

### Usage
```python
from openclaw import Strategy
from openclaw.safety import create_veronica_safety_layer

# Create safety layer
safety = create_veronica_safety_layer(
    cooldown_fails=3,      # Circuit breaker after 3 consecutive fails
    cooldown_seconds=600,  # 10 minutes cooldown
)

# Create strategy with safety
strategy = Strategy(config={...}, safety_layer=safety)

# Execute (safety checks automatic)
result = strategy.execute(context)
if result.get("status") == "blocked":
    print("Execution blocked by circuit breaker")
```

### Features
- **Circuit breakers**: Automatic cooldown on repeated failures
- **SAFE_MODE**: Emergency halt that persists across restarts
- **Crash recovery**: State survives SIGKILL (hard kill)
- **Zero overhead**: No-op safety layer (default) has zero performance cost

For more details, see [VERONICA Core documentation](https://github.com/amabito/veronica-core).
```

**Lines added**: ~30

---

## Total Changes Summary

| File | Lines Added | Breaking Changes |
|------|-------------|------------------|
| `setup.py` or `pyproject.toml` | 1 | No |
| `openclaw/safety.py` (new) | 70 | No |
| `openclaw/strategy.py` | 5 | No (backward compatible) |
| `README.md` | 30 | No |
| **Total** | **~106 lines** | **No breaking changes** |

---

## Backward Compatibility

### Existing Code (No Changes Required)

```python
# Works exactly as before
strategy = Strategy(config={...})
result = strategy.execute(context)
```

### New Code (Opt-in Safety)

```python
# New: with safety layer
from openclaw.safety import create_veronica_safety_layer

safety = create_veronica_safety_layer()
strategy = Strategy(config={...}, safety_layer=safety)
result = strategy.execute(context)
```

**Key**: `safety_layer` parameter is optional (defaults to `NoOpSafetyLayer()`), so existing code continues to work without changes.

---

## Testing Strategy

### Unit Tests

Add tests for safety integration:

```python
# tests/test_safety_integration.py
import pytest
from openclaw import Strategy
from openclaw.safety import NoOpSafetyLayer, create_veronica_safety_layer


def test_no_op_safety_layer():
    """Test that NoOpSafetyLayer has zero overhead."""
    safety = NoOpSafetyLayer()
    assert safety.is_in_cooldown("test") == False
    safety.record_pass("test")  # No-op
    safety.record_fail("test")  # No-op


@pytest.mark.skipif(
    not veronica_installed(),
    reason="VERONICA not installed",
)
def test_veronica_safety_layer():
    """Test VERONICA safety layer integration."""
    safety = create_veronica_safety_layer(cooldown_fails=3)

    # Not in cooldown initially
    assert safety.is_in_cooldown("test") == False

    # Trigger circuit breaker (3 consecutive fails)
    safety.record_fail("test")
    safety.record_fail("test")
    cooldown = safety.record_fail("test")

    # Circuit breaker should activate
    assert cooldown == True
    assert safety.is_in_cooldown("test") == True


def test_strategy_with_safety():
    """Test Strategy class with safety layer."""
    safety = NoOpSafetyLayer()
    strategy = Strategy(config={}, safety_layer=safety)

    # Execute should work normally
    result = strategy.execute({})
    assert "status" in result


def veronica_installed():
    try:
        import veronica_core
        return True
    except ImportError:
        return False
```

---

## Migration Path

### Phase 1: Add Interface (Non-Breaking)
1. Add `openclaw/safety.py` (new file)
2. Add `safety_layer` optional parameter to `Strategy.__init__()`
3. Add safety checks in `Strategy.execute()` (with default no-op)
4. Add `extras_require` to setup.py

**Result**: Existing code works unchanged. New users can opt-in to safety.

### Phase 2: Documentation
1. Update README with safety layer section
2. Add examples (`examples/with_safety.py`)
3. Add to migration guide (for existing users)

**Result**: Users aware of safety option, can migrate at their pace.

### Phase 3: Gradual Adoption
1. Recommend safety layer for production deployments (in docs)
2. Add metrics showing benefits (circuit breaker activation stats)
3. Collect user feedback

**Result**: Adoption grows organically, no forced migration.

---

## Alternative: External Integration (No OpenClaw Changes)

If OpenClaw maintainers prefer not to modify OpenClaw codebase, users can integrate externally:

**External wrapper** (no OpenClaw changes required):
```python
from openclaw import Strategy
from veronica_core import VeronicaIntegration


class SafeOpenClawStrategy:
    """External wrapper (no OpenClaw changes needed)."""

    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.veronica = VeronicaIntegration(cooldown_fails=3)

    def execute(self, context: dict) -> dict:
        if self.veronica.is_in_cooldown("strategy"):
            return {"status": "blocked"}

        try:
            result = self.strategy.execute(context)
            self.veronica.record_pass("strategy")
            return result
        except Exception as e:
            self.veronica.record_fail("strategy")
            raise


# Usage
strategy = Strategy(config={...})
safe_strategy = SafeOpenClawStrategy(strategy)
result = safe_strategy.execute(context)
```

**Trade-offs**:
- ✅ No OpenClaw changes required
- ✅ Works today (no waiting for OpenClaw release)
- ❌ Users must manage wrapper themselves
- ❌ Less discoverable (not in OpenClaw docs)

---

## Recommendation

**Preferred**: Phase 1-3 integration (add to OpenClaw codebase)
- Small code change (~106 lines)
- No breaking changes
- Better discoverability
- Official support from OpenClaw

**Alternative**: External wrapper (if OpenClaw prefers not to integrate)
- Zero OpenClaw changes
- Works immediately
- Users manage wrapper

---

## Questions for OpenClaw Maintainers

1. **Approach preference**: Integrated (Phase 1-3) or external wrapper?
2. **API changes**: Is `safety_layer` parameter acceptable for `Strategy.__init__()`?
3. **Testing**: Should we add integration tests to OpenClaw's CI?
4. **Documentation**: Should we add examples to OpenClaw examples directory?
5. **Release**: Target release version for integration (if accepted)?

We're happy to implement whichever approach OpenClaw maintainers prefer. Our goal is to complement OpenClaw's excellent decision-making with execution safety, not to complicate the codebase.
