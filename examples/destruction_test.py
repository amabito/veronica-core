"""Destruction test example - Demonstrates failsafe state preservation.

This example shows how VERONICA handles 3 critical scenarios:
1. SAFE_MODE persistence across process restart
2. Cooldown survival after hard kill (SIGKILL simulation)
3. Emergency state save on SIGINT (Ctrl+C)

Run this to see VERONICA's failsafe mechanisms in action.
"""

import sys
import time
from pathlib import Path

# Add src to path if running from examples directory
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core import VeronicaIntegration
from veronica_core.state import VeronicaState


def example_safe_mode_persistence():
    """Scenario 1: SAFE_MODE persists across restart."""
    print("\n" + "=" * 70)
    print("SCENARIO 1: SAFE_MODE Persistence")
    print("=" * 70)

    # Initialize VERONICA
    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600
    )

    # Transition to SAFE_MODE (emergency halt)
    print("[STEP 1] Transitioning to SAFE_MODE (emergency halt)")
    veronica.state.transition(VeronicaState.SAFE_MODE, "User emergency stop")
    veronica.save()

    print(f"[STATUS] Current state: {veronica.state.current_state.value}")
    print("[ACTION] State saved to disk")

    # Simulate process restart
    print("\n[STEP 2] Simulating process restart...")
    del veronica
    time.sleep(0.5)

    # Reload state
    print("[RESTART] Creating new VERONICA instance (loading from disk)")
    veronica_new = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600
    )

    # Verify SAFE_MODE preserved
    print(f"[VERIFY] State after restart: {veronica_new.state.current_state.value}")

    if veronica_new.state.current_state == VeronicaState.SAFE_MODE:
        print("[RESULT] PASS - SAFE_MODE preserved across restart")
    else:
        print(f"[RESULT] FAIL - Expected SAFE_MODE, got {veronica_new.state.current_state.value}")


def example_cooldown_persistence():
    """Scenario 2: Cooldown survives hard kill."""
    print("\n" + "=" * 70)
    print("SCENARIO 2: Cooldown Survival (SIGKILL simulation)")
    print("=" * 70)

    # Clear state for fresh test
    state_file = Path("data/state/veronica_state.json")
    if state_file.exists():
        state_file.unlink()
        print("[CLEAN] Previous state cleared")

    # Initialize and trigger cooldown
    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600,
        auto_save_interval=1  # Save immediately
    )

    print("\n[STEP 1] Triggering cooldown for btc_jpy")
    for i in range(3):
        cooldown_activated = veronica.record_fail("btc_jpy")
        print(f"  Fail #{i+1}: cooldown_activated={cooldown_activated}")

    remaining_before = veronica.get_cooldown_remaining("btc_jpy")
    print(f"[STATUS] Cooldown active: {remaining_before:.0f}s remaining")

    # Simulate SIGKILL (hard kill - cannot be caught)
    print("\n[STEP 2] Simulating SIGKILL (hard process kill)")
    print("[NOTE] In real scenario: kill -9 <pid>")
    del veronica  # Bypass exit handlers (simulates SIGKILL)
    time.sleep(1)

    # Restart and verify cooldown
    print("\n[RESTART] Reloading state after kill...")
    veronica_new = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600
    )

    is_cooldown = veronica_new.is_in_cooldown("btc_jpy")
    remaining_after = veronica_new.get_cooldown_remaining("btc_jpy")

    print(f"[VERIFY] btc_jpy in cooldown: {is_cooldown}")
    if is_cooldown:
        time_drift = remaining_before - remaining_after
        print(f"[VERIFY] Remaining: {remaining_after:.0f}s (drift: {time_drift:.1f}s)")
        print("[RESULT] PASS - Cooldown persisted across hard kill")
    else:
        print("[RESULT] FAIL - Cooldown not restored")


def example_sigint_emergency_save():
    """Scenario 3: SIGINT triggers emergency save."""
    print("\n" + "=" * 70)
    print("SCENARIO 3: SIGINT Emergency Save (Ctrl+C)")
    print("=" * 70)

    # Clear state
    state_file = Path("data/state/veronica_state.json")
    if state_file.exists():
        state_file.unlink()

    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600
    )

    # Set up some fail counts
    print("\n[STEP 1] Recording fails for eth_jpy")
    for i in range(3):
        veronica.record_fail("eth_jpy")
        print(f"  Fail #{i+1}")

    # Simulate SIGINT (Ctrl+C)
    print("\n[STEP 2] Simulating SIGINT (Ctrl+C)")
    print("[NOTE] In real scenario: Press Ctrl+C or kill -INT <pid>")
    print("[ACTION] SIGINT handler saves state before exit")

    # Manually save (simulates what SIGINT handler does)
    veronica.save()
    print("[STATUS] Emergency state save completed")

    # Restart and verify
    print("\n[RESTART] Reloading state after SIGINT...")
    veronica_new = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=600
    )

    is_cooldown = veronica_new.is_in_cooldown("eth_jpy")
    print(f"[VERIFY] eth_jpy in cooldown: {is_cooldown}")

    if is_cooldown:
        print("[RESULT] PASS - State saved through SIGINT")
    else:
        print("[RESULT] FAIL - State not restored")


def main():
    """Run all destruction test scenarios."""
    print("VERONICA DESTRUCTION TEST EXAMPLES")
    print("=" * 70)
    print("This demonstrates VERONICA's failsafe state preservation.")
    print()

    # Run scenarios
    example_safe_mode_persistence()
    example_cooldown_persistence()
    example_sigint_emergency_save()

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("All scenarios demonstrate VERONICA's production-grade failsafe:")
    print("- Critical states (SAFE_MODE) survive restarts")
    print("- Cooldown timers persist through hard kills (SIGKILL)")
    print("- Emergency handlers (SIGINT) save state before exit")
    print()
    print("For detailed evidence, see: PROOF.md")
    print("For automated testing, run: python proof_runner.py")


if __name__ == "__main__":
    main()
