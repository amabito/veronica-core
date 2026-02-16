"""OpenClaw Integration Demo - Strategy Engine + Safety Layer.

Demonstrates how VERONICA Core provides a failsafe execution layer
on top of powerful strategy engines like OpenClaw.

OpenClaw: High-performance autonomous agent framework
VERONICA: Execution safety layer (circuit breakers, SAFE_MODE, persistence)

This example shows:
1. Strategy engine makes decisions (what to do)
2. VERONICA enforces safety (how to execute safely)
3. Circuit breaker activates on repeated failures
4. SAFE_MODE prevents runaway execution
5. State persists across restarts
"""

from veronica_core import VeronicaIntegration, MemoryBackend
from veronica_core.state import VeronicaState
from typing import Dict, Any
import random


# ========================================
# Dummy OpenClaw-style Strategy Engine
# ========================================

class StrategyEngine:
    """Simulates a high-frequency strategy engine (OpenClaw-style).

    This represents ANY powerful strategy engine that makes autonomous decisions.
    VERONICA sits ABOVE this layer to provide execution safety.
    """

    def __init__(self, name: str = "StrategyEngine"):
        self.name = name
        self.decision_count = 0

    def decide(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Make strategic decision (what to do).

        Args:
            context: Current market/system state

        Returns:
            Decision dict with action and metadata
        """
        self.decision_count += 1

        # Simulate strategy logic (could be OpenClaw, custom ML, etc.)
        # For demo: intentionally risky decisions to show safety layer
        risk_score = random.uniform(0, 1)

        return {
            "action": "execute_trade" if risk_score > 0.3 else "skip",
            "risk_score": risk_score,
            "decision_id": f"{self.name}_decision_{self.decision_count}",
            "metadata": context,
        }


# ========================================
# VERONICA Safety Layer Integration
# ========================================

class SafeStrategyExecutor:
    """Wraps strategy engine with VERONICA safety layer.

    Architecture:
        Strategy Engine (OpenClaw, etc.) → decides WHAT to do
        VERONICA Core → enforces HOW to execute safely
        External System → WHERE it runs
    """

    def __init__(self, strategy: StrategyEngine):
        self.strategy = strategy
        self.backend = MemoryBackend()

        # VERONICA safety layer
        self.veronica = VeronicaIntegration(
            cooldown_fails=3,       # Circuit breaker: 3 consecutive fails
            cooldown_seconds=60,    # 1 minute cooldown
            auto_save_interval=1,   # Save after every operation
            backend=self.backend,
        )

        print(f"[SafeStrategyExecutor] Initialized with {strategy.name}")
        print(f"  Safety layer: Circuit breaker (3 fails) + SAFE_MODE + Persistence")

    def execute_strategy(self, context: Dict[str, Any]) -> bool:
        """Execute strategy decision with safety checks.

        Returns:
            True if executed successfully, False if blocked by safety layer
        """
        entity_id = "strategy_execution"

        # Safety check 1: Circuit breaker
        if self.veronica.is_in_cooldown(entity_id):
            remaining = self.veronica.get_cooldown_remaining(entity_id)
            print(f"  [SAFETY BLOCK] Circuit breaker active: {remaining:.0f}s remaining")
            return False

        # Safety check 2: SAFE_MODE
        if self.veronica.state.current_state == VeronicaState.SAFE_MODE:
            print(f"  [SAFETY BLOCK] System in SAFE_MODE - all execution halted")
            return False

        # Get strategy decision
        decision = self.strategy.decide(context)
        print(f"\n[Strategy] Decision: {decision['action']} (risk: {decision['risk_score']:.2f})")

        # Execute with safety monitoring
        try:
            # Simulate execution
            success = self._simulate_execution(decision)

            if success:
                self.veronica.record_pass(entity_id)
                print(f"  [SUCCESS] Execution completed")
                return True
            else:
                # Record failure - may trigger circuit breaker
                cooldown_activated = self.veronica.record_fail(entity_id)
                fail_count = self.veronica.get_fail_count(entity_id)
                print(f"  [FAIL] Execution failed (fail count: {fail_count})")

                if cooldown_activated:
                    print(f"  [CIRCUIT BREAKER] Activated - system entering cooldown")

                return False

        except Exception as e:
            print(f"  [ERROR] Unexpected error: {e}")
            self.veronica.record_fail(entity_id)
            return False

    def _simulate_execution(self, decision: Dict) -> bool:
        """Simulate execution with probabilistic failure."""
        if decision["action"] == "skip":
            return True

        # Simulate: high risk = higher failure rate
        risk = decision["risk_score"]
        return random.random() > risk

    def trigger_safe_mode(self, reason: str):
        """Manually trigger SAFE_MODE (emergency halt)."""
        print(f"\n[EMERGENCY] Triggering SAFE_MODE: {reason}")
        self.veronica.state.transition(VeronicaState.SAFE_MODE, reason)
        self.veronica.save()
        print(f"  State saved - system will remain halted after restart")

    def get_stats(self) -> Dict:
        """Get current safety layer statistics."""
        return self.veronica.get_stats()


# ========================================
# Demo Scenarios
# ========================================

def demo_circuit_breaker():
    """Scenario 1: Circuit breaker activation."""
    print("=" * 70)
    print("SCENARIO 1: Circuit Breaker Activation")
    print("=" * 70)

    strategy = StrategyEngine("OpenClaw-Demo")
    executor = SafeStrategyExecutor(strategy)

    # Run strategy until circuit breaker activates
    context = {"market": "volatile"}

    for i in range(10):
        print(f"\n--- Execution Attempt {i+1} ---")
        success = executor.execute_strategy(context)

        if not success and executor.veronica.is_in_cooldown("strategy_execution"):
            print("\n[RESULT] Circuit breaker activated - protecting system")
            break

    # Show final stats
    stats = executor.get_stats()
    print(f"\n[Stats] State: {stats['current_state']}")
    print(f"[Stats] Active Cooldowns: {stats['active_cooldowns']}")
    print(f"[Stats] Fail Counts: {stats['fail_counts']}")


def demo_safe_mode_persistence():
    """Scenario 2: SAFE_MODE persistence across restart."""
    print("\n" + "=" * 70)
    print("SCENARIO 2: SAFE_MODE Persistence (Emergency Halt)")
    print("=" * 70)

    strategy = StrategyEngine("OpenClaw-Demo")
    executor = SafeStrategyExecutor(strategy)

    # Trigger emergency halt
    executor.trigger_safe_mode("Manual emergency stop - anomaly detected")

    # Simulate restart
    print("\n[RESTART] Simulating system restart...")
    del executor

    # Create new executor (loads persisted state)
    strategy_new = StrategyEngine("OpenClaw-Demo")
    executor_new = SafeStrategyExecutor(strategy_new)

    # Verify SAFE_MODE persisted
    state = executor_new.veronica.state.current_state
    print(f"\n[VERIFY] State after restart: {state.value}")

    if state == VeronicaState.SAFE_MODE:
        print(f"  ✅ SAFE_MODE persisted - system remains halted")
        print(f"  ✅ Strategy engine cannot execute until operator clears state")
    else:
        print(f"  ❌ State not preserved (unexpected)")

    # Try to execute (should be blocked)
    print(f"\n[TEST] Attempting execution in SAFE_MODE...")
    success = executor_new.execute_strategy({"market": "normal"})
    print(f"  Execution result: {'allowed' if success else 'blocked'}")


def demo_strategy_safety_separation():
    """Scenario 3: Strategy engine + Safety layer independence."""
    print("\n" + "=" * 70)
    print("SCENARIO 3: Strategy/Safety Layer Separation")
    print("=" * 70)

    print("\n[PRINCIPLE] Strategy engine decides WHAT to do")
    print("[PRINCIPLE] VERONICA enforces HOW to execute safely")
    print("[PRINCIPLE] Strategy can be swapped without changing safety layer")

    # Demo: Multiple strategy engines with same safety layer
    strategies = [
        StrategyEngine("OpenClaw-Aggressive"),
        StrategyEngine("OpenClaw-Conservative"),
        StrategyEngine("CustomStrategy"),
    ]

    for strategy in strategies:
        print(f"\n--- Testing with {strategy.name} ---")
        executor = SafeStrategyExecutor(strategy)

        # All strategies use the same safety layer
        for _ in range(2):
            executor.execute_strategy({"market": "test"})

        stats = executor.get_stats()
        print(f"  Safety layer state: {stats['current_state']}")


# ========================================
# Main Demo
# ========================================

def main():
    """Run all integration demo scenarios."""
    print("\n" + "=" * 70)
    print("VERONICA Core + OpenClaw Integration Demo")
    print("=" * 70)
    print("\nArchitecture:")
    print("  Strategy Engine (OpenClaw) → Decides WHAT to do")
    print("  VERONICA Core             → Enforces HOW to execute safely")
    print("  External System           → WHERE it runs")
    print("\n" + "=" * 70)

    # Run scenarios
    demo_circuit_breaker()
    demo_safe_mode_persistence()
    demo_strategy_safety_separation()

    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
    print("\nKey Takeaways:")
    print("  1. Strategy engines provide decisions (WHAT)")
    print("  2. VERONICA provides execution safety (HOW)")
    print("  3. Circuit breaker prevents runaway failures")
    print("  4. SAFE_MODE emergency halt persists across restarts")
    print("  5. Strategy and safety layers are independent")
    print("\nConclusion:")
    print("  Powerful strategy engines like OpenClaw benefit from")
    print("  a failsafe execution layer. VERONICA provides that layer.")


if __name__ == "__main__":
    main()
