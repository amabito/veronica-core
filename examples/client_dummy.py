"""Example: Using DummyClient for testing.

Demonstrates LLM client injection with a simple dummy client.
DummyClient returns fixed responses - useful for unit tests.
"""

from veronica_core import VeronicaIntegration, DummyClient


def main():
    # Initialize with DummyClient (returns "SAFE" for all prompts)
    client = DummyClient(fixed_response="SAFE")

    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,
        client=client,
    )

    print("=== VERONICA with DummyClient ===\n")

    # Example: Use LLM for decision (returns fixed "SAFE")
    task = "high_risk_operation"
    prompt = f"Is {task} safe to execute? Reply SAFE or UNSAFE."
    response = veronica.client.generate(prompt)
    print(f"LLM decision: {response}")

    if response == "SAFE":
        print(f"Executing {task}...")
        # Simulate task execution
        success = True
        if success:
            veronica.record_pass(task)
            print("Task succeeded\n")
    else:
        print(f"Task {task} rejected by LLM\n")

    # Check client statistics
    print(f"LLM call count: {client.call_count}")
    print(f"Last prompt: {client.last_prompt}")

    # VERONICA state
    stats = veronica.get_stats()
    print(f"\nVERONICA state: {stats['current_state']}")
    print(f"Fail counts: {stats['fail_counts']}")


if __name__ == "__main__":
    main()
