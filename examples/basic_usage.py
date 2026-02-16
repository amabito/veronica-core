"""Basic usage example for VERONICA Core - Deterministic demo (always succeeds)."""

from veronica_core import VeronicaIntegration


def main():
    """Demonstrate VERONICA Core basics with deterministic flow."""
    print("=== VERONICA Core Basic Usage Demo ===\n")

    # Initialize with defaults
    veronica = VeronicaIntegration(
        cooldown_fails=3,       # Cooldown after 3 consecutive fails
        cooldown_seconds=60,    # 1 minute cooldown (reduced for demo)
        auto_save_interval=10   # Auto-save every 10 operations
    )

    # Scenario 1: Successful operations (no cooldown)
    print("--- Scenario 1: Successful Operations ---")
    task_a = "api_call_users"

    for i in range(3):
        print(f"Attempt {i+1}: Calling API...")
        # Simulate successful API call
        veronica.record_pass(task_a)
        print(f"  Success! (Fail count reset)")

    print(f"Cooldown status: {veronica.is_in_cooldown(task_a)}")  # False

    # Scenario 2: Trigger cooldown (3 consecutive fails)
    print("\n--- Scenario 2: Circuit Breaker Activation ---")
    task_b = "flaky_service"

    for i in range(3):
        print(f"Attempt {i+1}: Calling flaky service...")
        cooldown_activated = veronica.record_fail(task_b)
        fail_count = veronica.get_fail_count(task_b)
        print(f"  Failed! (Fail count: {fail_count})")

        if cooldown_activated:
            print(f"  >>> Circuit breaker ACTIVATED for {task_b}")

    # Verify cooldown
    is_cooldown = veronica.is_in_cooldown(task_b)
    remaining = veronica.get_cooldown_remaining(task_b)
    print(f"\nCooldown active: {is_cooldown}")
    print(f"Remaining: {remaining:.0f}s")

    # Scenario 3: State persistence
    print("\n--- Scenario 3: State Persistence ---")
    print("Saving state to disk...")
    veronica.save()

    # Get statistics
    stats = veronica.get_stats()
    print(f"\nFinal State: {stats['current_state']}")
    print(f"Active Cooldowns: {len(stats['active_cooldowns'])}")
    print(f"Fail Counts: {stats['fail_counts']}")

    print("\n=== Demo Complete ===")
    print("State persisted. Restart will restore cooldowns.")


if __name__ == "__main__":
    main()
