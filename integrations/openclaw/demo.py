"""OpenClaw + VERONICA Integration Demo.

Demonstrates VERONICA safety layer wrapping OpenClaw strategy engine.

If OpenClaw is not installed, this demo uses a mock strategy engine to
demonstrate the integration pattern.

Usage:
    python integrations/openclaw/demo.py
"""

import sys
import time
from typing import Dict, Any

# Try to import OpenClaw (if installed)
try:
    from openclaw import Strategy as OpenClawStrategy  # type: ignore

    OPENCLAW_AVAILABLE = True
    print("[INFO] OpenClaw detected - using real OpenClaw strategy")
except ImportError:
    OPENCLAW_AVAILABLE = False
    print("[INFO] OpenClaw not installed - using mock strategy for demo")
    print("[INFO] Install OpenClaw: pip install openclaw")

# Import VERONICA integration
sys.path.insert(0, ".")
from integrations.openclaw.adapter import SafeOpenClawExecutor
from veronica_core.state import VeronicaState


# ========================================
# Mock OpenClaw Strategy (Fallback)
# ========================================


class MockOpenClawStrategy:
    """Mock OpenClaw strategy for demo purposes.

    Simulates OpenClaw's strategy engine API with configurable failure rate.
    """

    def __init__(self, fail_rate: float = 0.3):
        """Initialize mock strategy.

        Args:
            fail_rate: Probability of failure (0.0-1.0)
        """
        self.fail_rate = fail_rate
        self.call_count = 0

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate strategy execution (OpenClaw API).

        Args:
            context: Execution context

        Returns:
            Strategy result

        Raises:
            RuntimeError: If execution fails (simulated)
        """
        self.call_count += 1

        # Simulate failure based on fail_rate
        import random

        if random.random() < self.fail_rate:
            raise RuntimeError(f"Strategy execution failed (call #{self.call_count})")

        return {
            "action": "execute_trade",
            "signal": "BUY",
            "confidence": 0.85,
            "call_count": self.call_count,
        }


# ========================================
# Demo Scenarios
# ========================================


def demo_circuit_breaker():
    """Scenario 1: Circuit breaker activation on repeated failures."""
    print("\n" + "=" * 70)
    print("SCENARIO 1: Circuit Breaker Activation")
    print("=" * 70)

    # Create strategy (high failure rate to trigger circuit breaker quickly)
    if OPENCLAW_AVAILABLE:
        strategy = OpenClawStrategy()  # type: ignore
    else:
        strategy = MockOpenClawStrategy(fail_rate=0.8)  # 80% failure rate

    # Wrap with VERONICA safety
    executor = SafeOpenClawExecutor(strategy, cooldown_fails=3, cooldown_seconds=60)

    # Execute until circuit breaker activates
    context = {"market": "demo"}

    for i in range(10):
        print(f"\n--- Execution Attempt {i+1} ---")

        result = executor.safe_execute(context)

        if result["status"] == "success":
            print(f"  [SUCCESS] {result['data']}")
        elif result["status"] == "failed":
            print(f"  [FAILED] {result['reason']}")
        elif result["status"] == "blocked":
            print(f"  [BLOCKED] {result['reason']}")
            print("\n[RESULT] Circuit breaker activated - protecting system")
            break

    # Show final status
    status = executor.get_status()
    print(f"\n[Status] State: {status['state']}")
    print(f"[Status] Fail count: {status['fail_count']}")
    print(f"[Status] In cooldown: {status['in_cooldown']}")


def demo_safe_mode_persistence():
    """Scenario 2: SAFE_MODE persistence across restart."""
    print("\n" + "=" * 70)
    print("SCENARIO 2: SAFE_MODE Persistence (Emergency Halt)")
    print("=" * 70)

    # Create strategy
    if OPENCLAW_AVAILABLE:
        strategy = OpenClawStrategy()  # type: ignore
    else:
        strategy = MockOpenClawStrategy(fail_rate=0.0)  # No failures

    # Wrap with VERONICA
    executor = SafeOpenClawExecutor(strategy)

    # Trigger emergency halt
    print("\n[ACTION] Triggering SAFE_MODE (emergency halt)...")
    executor.trigger_safe_mode("Manual emergency stop - anomaly detected")

    # Simulate restart
    print("\n[RESTART] Simulating system restart...")
    del executor

    # Create new executor (loads persisted state)
    if OPENCLAW_AVAILABLE:
        strategy_new = OpenClawStrategy()  # type: ignore
    else:
        strategy_new = MockOpenClawStrategy(fail_rate=0.0)

    executor_new = SafeOpenClawExecutor(strategy_new)

    # Verify SAFE_MODE persisted
    state = executor_new.veronica.state.current_state
    print(f"\n[VERIFY] State after restart: {state.value}")

    if state == VeronicaState.SAFE_MODE:
        print("  [OK] SAFE_MODE persisted - system remains halted")
        print("  [OK] Strategy cannot execute until operator clears state")
    else:
        print("  [NG] State not preserved (unexpected)")

    # Try to execute (should be blocked)
    print("\n[TEST] Attempting execution in SAFE_MODE...")
    result = executor_new.safe_execute({"market": "demo"})
    print(f"  Result: {result['status']}")
    print(f"  Reason: {result['reason']}")

    # Clear SAFE_MODE for next demo
    executor_new.clear_safe_mode("Demo complete - clearing SAFE_MODE")


def demo_integration_pattern():
    """Scenario 3: Typical integration pattern."""
    print("\n" + "=" * 70)
    print("SCENARIO 3: Typical Integration Pattern")
    print("=" * 70)

    # Create strategy
    if OPENCLAW_AVAILABLE:
        strategy = OpenClawStrategy()  # type: ignore
    else:
        strategy = MockOpenClawStrategy(fail_rate=0.2)  # 20% failure rate

    # Wrap with VERONICA
    executor = SafeOpenClawExecutor(strategy, cooldown_fails=3, cooldown_seconds=60)

    print("\n[PATTERN] Strategy engine + VERONICA safety layer")
    print("  - Strategy decides WHAT to do")
    print("  - VERONICA enforces HOW to execute safely")

    # Typical execution loop
    context = {"market": "demo", "timestamp": time.time()}

    for i in range(5):
        print(f"\n--- Iteration {i+1} ---")

        result = executor.safe_execute(context)

        if result["status"] == "blocked":
            # Circuit breaker or SAFE_MODE active
            print(f"  Execution blocked: {result['reason']}")
            time.sleep(1)
            continue

        if result["status"] == "failed":
            # Execution failed (not yet in cooldown)
            print(f"  Execution failed: {result['reason']}")
            print("  VERONICA recorded failure - will trigger circuit breaker if pattern continues")
            continue

        if result["status"] == "success":
            # Execution successful
            print(f"  Execution successful: {result['data']}")
            print("  VERONICA reset fail counter")

    # Show final status
    status = executor.get_status()
    print(f"\n[Final Status]")
    print(f"  State: {status['state']}")
    print(f"  Fail count: {status['fail_count']}")
    print(f"  In cooldown: {status['in_cooldown']}")


# ========================================
# Main Demo
# ========================================


def main():
    """Run all integration demo scenarios."""
    print("\n" + "=" * 70)
    print("OpenClaw + VERONICA Integration Demo")
    print("=" * 70)

    if OPENCLAW_AVAILABLE:
        print("\n[MODE] Using real OpenClaw strategy")
    else:
        print("\n[MODE] Using mock OpenClaw strategy (install openclaw for real integration)")

    print("\nThis demo shows:")
    print("  1. Circuit breaker activation on repeated failures")
    print("  2. SAFE_MODE persistence across restart")
    print("  3. Typical integration pattern")

    # Run scenarios
    demo_circuit_breaker()
    demo_safe_mode_persistence()
    demo_integration_pattern()

    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
    print("\nKey Takeaways:")
    print("  - OpenClaw decides WHAT to do (strategy logic)")
    print("  - VERONICA enforces HOW to execute safely (circuit breakers, emergency halt)")
    print("  - Both layers are independent and complementary")
    print("\nNext Steps:")
    print("  1. Review integration code: integrations/openclaw/adapter.py")
    print("  2. Read integration guide: integrations/openclaw/README.md")
    print("  3. Integrate with your OpenClaw strategy")


if __name__ == "__main__":
    main()
