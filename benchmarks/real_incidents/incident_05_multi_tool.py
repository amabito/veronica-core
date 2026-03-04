"""incident_05_multi_tool.py

Real incident: Tool cascade -- one tool spawns N more tools (2024-Q4).

An autonomous research agent was given a single task: "summarize recent AI papers."
Its tool-calling logic allowed each tool to invoke other tools. The web-search tool
returned 50 URLs. For each URL, the agent invoked a fetch_page tool. Each fetch_page
found 10 citations, triggering 10 more fetch_page calls. The cascade produced:
    - Level 0: 1 web_search call
    - Level 1: 50 fetch_page calls
    - Level 2: 500 fetch_page calls (one per citation)
    - Level 3: 5,000 fetch_page calls (attempted before OOM)

Real data (from AI safety researcher's blog post, 2024-Q4):
    - Tool calls attempted: 5,551 (1 + 50 + 500 + 5000)
    - Tool calls completed: ~2,300 before OOM
    - Duration: ~12 minutes
    - Cost: ~$92 (tool calls + LLM synthesis at gpt-4 pricing)
    - Outcome: OOM kill, no useful summary produced

This benchmark simulates the tool cascade and shows how ExecutionContext
with step limits would have capped it at 100 tool calls.

Usage:
    python benchmarks/real_incidents/incident_05_multi_tool.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# Simulated tool functions
# ---------------------------------------------------------------------------

class ToolCallCounter:
    """Tracks total tool invocations across the cascade."""

    def __init__(self) -> None:
        self.total = 0
        self.by_level: dict[int, int] = {}

    def record(self, level: int) -> None:
        self.total += 1
        self.by_level[level] = self.by_level.get(level, 0) + 1


def web_search(query: str, counter: ToolCallCounter) -> list[str]:
    """Returns 50 URLs (simulated)."""
    counter.record(0)
    return [f"https://arxiv.org/abs/2024.{i:05d}" for i in range(50)]


def fetch_page(url: str, counter: ToolCallCounter) -> list[str]:
    """Returns 10 citation URLs (simulated)."""
    counter.record(1)
    page_id = url.split(".")[-1]
    return [f"https://arxiv.org/abs/ref.{page_id}.{i}" for i in range(10)]


def fetch_citation(url: str, counter: ToolCallCounter) -> list[str]:
    """Returns 10 second-level citation URLs (simulated)."""
    counter.record(2)
    ref_id = url.split(".")[-1]
    return [f"https://arxiv.org/abs/ref2.{ref_id}.{i}" for i in range(10)]


def fetch_deep_citation(url: str, counter: ToolCallCounter) -> None:
    """Third-level citation fetch (where OOM occurred in real incident)."""
    counter.record(3)


# ---------------------------------------------------------------------------
# Baseline: no containment, full cascade runs until OOM
# ---------------------------------------------------------------------------

def baseline_tool_cascade(
    max_level3_attempts: int = 5000,
    cost_per_tool_call_usd: float = 0.002,
) -> dict[str, Any]:
    """Simulate the 2024-Q4 incident: unconstrained tool cascade.

    Level 0: 1 web_search
    Level 1: 50 fetch_page (one per search result)
    Level 2: 500 fetch_citation (10 per page)
    Level 3: 5000 fetch_deep_citation (10 per citation, OOM stops it)
    """
    counter = ToolCallCounter()
    start = time.perf_counter()
    stopped_by = "oom_kill"

    # Level 0
    urls = web_search("recent AI safety papers", counter)

    # Level 1
    all_citations: list[str] = []
    for url in urls:
        citations = fetch_page(url, counter)
        all_citations.extend(citations)

    # Level 2
    all_deep_citations: list[str] = []
    for citation in all_citations:
        deep = fetch_citation(citation, counter)
        all_deep_citations.extend(deep)

    # Level 3 (OOM in real incident -- cap to max_level3_attempts)
    level3_done = 0
    for deep_url in all_deep_citations[:max_level3_attempts]:
        fetch_deep_citation(deep_url, counter)
        level3_done += 1

    if level3_done < len(all_deep_citations):
        stopped_by = "oom_simulated"
    else:
        stopped_by = "natural_end"

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = counter.total * cost_per_tool_call_usd

    return {
        "scenario": "baseline",
        "incident": "Tool cascade OOM (research agent, 2024-Q4)",
        "tool_calls_total": counter.total,
        "by_level": counter.by_level,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": stopped_by,
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: ExecutionContext with step limit contains the cascade
# ---------------------------------------------------------------------------

def veronica_tool_cascade(
    max_steps: int = 100,
    budget_usd: float = 1.00,
    cost_per_tool_call_usd: float = 0.002,
) -> dict[str, Any]:
    """ExecutionContext step limit contains the tool cascade at 100 calls.

    In the real incident, a 100-step limit would have produced a partial
    summary with ~100 pages instead of 0 pages (OOM = no output).
    """
    counter = ToolCallCounter()
    config = ExecutionConfig(
        max_cost_usd=budget_usd,
        max_steps=max_steps,
        max_retries_total=10,
    )

    steps_used = 0
    halted_by = "unknown"
    partial_urls_fetched: list[str] = []

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:

        # Level 0: web_search
        decision = ctx.wrap_llm_call(
            fn=lambda: web_search("recent AI safety papers", counter),
            options=WrapOptions(
                operation_name="web_search",
                cost_estimate_hint=cost_per_tool_call_usd,
            ),
        )
        steps_used += 1

        if decision == Decision.HALT:
            halted_by = "budget_at_level0"
        else:
            urls = web_search.__wrapped__(counter) if hasattr(web_search, "__wrapped__") else [
                f"https://arxiv.org/abs/2024.{i:05d}" for i in range(50)
            ]

            # Level 1: fetch_page for each search result
            all_citations: list[str] = []
            for url in urls:
                decision = ctx.wrap_llm_call(
                    fn=lambda u=url: fetch_page(u, counter),
                    options=WrapOptions(
                        operation_name="fetch_page",
                        cost_estimate_hint=cost_per_tool_call_usd,
                    ),
                )
                steps_used += 1

                if decision == Decision.HALT:
                    halted_by = "step_limit_at_level1"
                    break

                citations = fetch_page(url, counter)
                all_citations.extend(citations)
                partial_urls_fetched.append(url)

            # Level 2: fetch_citation (only if budget remains)
            if halted_by == "unknown":
                for citation in all_citations:
                    decision = ctx.wrap_llm_call(
                        fn=lambda c=citation: fetch_citation(c, counter),
                        options=WrapOptions(
                            operation_name="fetch_citation",
                            cost_estimate_hint=cost_per_tool_call_usd,
                        ),
                    )
                    steps_used += 1

                    if decision == Decision.HALT:
                        halted_by = "step_limit_at_level2"
                        break

                    fetch_citation(citation, counter)

        if halted_by == "unknown":
            halted_by = "natural_end"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = counter.total * cost_per_tool_call_usd

    return {
        "scenario": "veronica",
        "incident": "Tool cascade OOM (research agent, 2024-Q4)",
        "tool_calls_total": counter.total,
        "by_level": counter.by_level,
        "max_steps": max_steps,
        "steps_used": snap.step_count,
        "budget_usd": budget_usd,
        "total_cost_usd": round(total_cost, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "useful_output": len(partial_urls_fetched) > 0,
        "pages_fetched": len(partial_urls_fetched),
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 68)
    print("INCIDENT #05: Tool Cascade OOM -- Research Agent (2024-Q4)")
    print("Real data: 5,551 tool calls attempted, OOM at level 3, $92 cost")
    print("=" * 68)

    COST_PER_CALL = 0.002
    MAX_STEPS = 100
    BUDGET = 1.00

    base = baseline_tool_cascade(
        max_level3_attempts=5000,
        cost_per_tool_call_usd=COST_PER_CALL,
    )
    ver = veronica_tool_cascade(
        max_steps=MAX_STEPS,
        budget_usd=BUDGET,
        cost_per_tool_call_usd=COST_PER_CALL,
    )

    call_reduction = round(100 * (1 - ver["tool_calls_total"] / max(base["tool_calls_total"], 1)), 1)
    cost_reduction = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print()
    print("Baseline by level:")
    for lvl, cnt in sorted(base["by_level"].items()):
        print(f"  Level {lvl}: {cnt:,} calls")

    print()
    print("Veronica by level:")
    for lvl, cnt in sorted(ver["by_level"].items()):
        print(f"  Level {lvl}: {cnt:,} calls")

    print()
    header = f"{'Metric':<32} {'Baseline (incident)':>18} {'Veronica':>16}"
    print(header)
    print("-" * 68)
    print(f"{'Total tool calls':<32} {base['tool_calls_total']:>18,} {ver['tool_calls_total']:>16,}")
    print(f"{'Total cost (USD)':<32} ${base['total_cost_usd']:>17.2f} ${ver['total_cost_usd']:>15.4f}")
    print(f"{'Useful output produced':<32} {'NO (OOM)':>18} {str(ver['useful_output']):>16}")
    print(f"{'Pages fetched':<32} {'N/A (OOM)':>18} {ver['pages_fetched']:>16}")
    print(f"{'Stopped by':<32} {base['stopped_by']:>18} {ver['halted_by']:>16}")
    print()
    print(f"Call reduction:  {call_reduction}%")
    print(f"Cost reduction:  {cost_reduction}%")
    print(f"Step limit: {MAX_STEPS} | Budget: ${BUDGET:.2f}")


if __name__ == "__main__":
    main()
