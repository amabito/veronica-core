"""run_all.py -- Run all real incident benchmarks and print a summary table.

Usage:
    python benchmarks/real_incidents/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
import time


INCIDENTS = [
    ("incident_01_openai_loop", "GPT-4 Self-Correction Loop (2023-Q3)"),
    ("incident_02_cost_spike", "$552 Recursive Agent Bill (2024-Q1)"),
    ("incident_03_websocket_ddos", "47k tok/s WebSocket Flood (2024-Q2)"),
    ("incident_04_semantic_echo", "Semantic Echo Chamber (2024-Q3)"),
    ("incident_05_multi_tool", "Tool Cascade OOM (2024-Q4)"),
]


def run_incident(module: str) -> tuple[int, float]:
    """Run a single incident benchmark. Returns (exit_code, elapsed_sec)."""
    start = time.perf_counter()
    result = subprocess.run(
        [sys.executable, f"benchmarks/real_incidents/{module}.py"],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - start
    if result.returncode != 0:
        print(f"\n[FAIL] {module}:\n{result.stderr[-500:]}")
    else:
        # Print the benchmark output indented
        for line in result.stdout.splitlines():
            print(f"  {line}")
    return result.returncode, elapsed


def main() -> None:
    print("=" * 72)
    print("VERONICA REAL INCIDENT BENCHMARKS -- Full Suite")
    print("=" * 72)

    results: list[tuple[str, str, int, float]] = []

    for module, label in INCIDENTS:
        print(f"\n--- {label} ---")
        code, elapsed = run_incident(module)
        results.append((module, label, code, elapsed))

    # Summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"{'Incident':<50} {'Status':>8} {'Time (s)':>10}")
    print("-" * 72)
    all_passed = True
    for module, label, code, elapsed in results:
        status = "PASS" if code == 0 else "FAIL"
        if code != 0:
            all_passed = False
        print(f"{label:<50} {status:>8} {elapsed:>10.2f}")

    print()
    if all_passed:
        print("All incidents: PASS")
    else:
        print("Some incidents FAILED -- see output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
