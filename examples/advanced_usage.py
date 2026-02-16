"""Advanced usage example - Custom guard, backend, and LLM client."""

from veronica_core import (
    VeronicaIntegration,
    VeronicaGuard,
    JSONBackend,
    DummyClient,
)
from typing import Dict, Any


class ApiGuard(VeronicaGuard):
    """Custom guard for API monitoring with context-aware cooldown logic."""

    def should_cooldown(self, entity: str, context: Dict[str, Any]) -> bool:
        """Activate cooldown if error rate is too high."""
        error_rate = context.get("error_rate", 0)
        response_time = context.get("response_time_ms", 0)

        # Early cooldown if error rate > 50%
        if error_rate > 0.5:
            print(f"[ApiGuard] High error rate ({error_rate:.0%}) - cooldown activated")
            return True

        # Early cooldown if response time > 5 seconds
        if response_time > 5000:
            print(f"[ApiGuard] Slow response ({response_time}ms) - cooldown activated")
            return True

        return False

    def validate_state(self, state_data: Dict[str, Any]) -> bool:
        """Validate that all entities are valid API endpoints."""
        valid_endpoints = {"/api/users", "/api/posts", "/api/comments"}
        fail_counts = state_data.get("fail_counts", {})

        for endpoint in fail_counts.keys():
            if endpoint not in valid_endpoints:
                print(f"[ApiGuard] Invalid endpoint '{endpoint}' in state")
                return False

        return True

    def on_cooldown_activated(self, entity: str, context: Dict[str, Any]) -> None:
        """Log when cooldown is activated."""
        print(f"[ApiGuard] Cooldown activated for {entity}: {context}")


def main():
    """Demonstrate advanced VERONICA features with pluggable components."""
    print("=== VERONICA Advanced Usage Demo ===\n")

    # 1. Custom backend (JSON file)
    backend = JSONBackend("data/state/advanced_demo.json")

    # 2. Custom guard (API monitoring)
    guard = ApiGuard()

    # 3. Custom LLM client (dummy for demo)
    client = DummyClient(fixed_response="HEALTHY")

    # Initialize with all custom components
    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,  # 1 minute for demo
        auto_save_interval=5,
        backend=backend,
        guard=guard,
        client=client,
    )

    # Scenario 1: Normal fails (no early cooldown)
    print("--- Scenario 1: Normal API Failures ---")
    endpoint = "/api/users"

    for i in range(2):
        context = {"error_rate": 0.2, "response_time_ms": 100}
        cooldown = veronica.record_fail(endpoint, context=context)
        print(f"  Fail {i+1}: cooldown={cooldown}")

    # Scenario 2: High error rate (early cooldown via guard)
    print("\n--- Scenario 2: High Error Rate Detection ---")
    context = {"error_rate": 0.7, "response_time_ms": 200}
    cooldown = veronica.record_fail(endpoint, context=context)
    print(f"  High error rate detected: cooldown={cooldown}")

    # Scenario 3: Use LLM for health check
    print("\n--- Scenario 3: LLM-Enhanced Decision ---")
    health_check = veronica.client.generate(f"Is {endpoint} healthy?")
    print(f"  LLM health check: {health_check}")

    # Scenario 4: Successful request (reset fail counter)
    print("\n--- Scenario 4: Recovery ---")
    veronica.record_pass(endpoint)
    print(f"  Success! Fail count reset: {veronica.get_fail_count(endpoint)}")

    # Final statistics
    print("\n--- Final Statistics ---")
    stats = veronica.get_stats()
    print(f"  State: {stats['current_state']}")
    print(f"  Active Cooldowns: {len(stats['active_cooldowns'])}")
    print(f"  Fail Counts: {stats['fail_counts']}")

    # Save state
    veronica.save()
    print(f"\n  State saved to {backend.path}")

    print("\n=== Demo Complete ===")
    print("Demonstrated: Custom Guard + Custom Backend + LLM Client")


if __name__ == "__main__":
    main()
