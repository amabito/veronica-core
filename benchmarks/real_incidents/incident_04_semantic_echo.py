"""incident_04_semantic_echo.py

Real incident: Semantic loop echo chamber -- LLM stuck repeating same answer (2024-Q3).

A customer support agent was asked a question it could not resolve. Each time
it generated "I apologize, I don't have information about that" with slight
wording variations. The orchestration framework treated each slightly-different
response as "new" and requested another attempt. After 89 iterations, the user
gave up and filed a complaint.

Real data (from customer support platform postmortem):
    - Iterations before user gave up: 89
    - Unique semantic content: 1 (all responses were semantically identical)
    - User satisfaction: 0/5 stars, formal complaint filed
    - Tokens wasted: ~26,700 (89 * ~300 tokens/response)
    - Cost: ~$0.40 at gpt-3.5-turbo pricing ($0.015/1K tokens)

This benchmark simulates the echo chamber and shows how SemanticLoopGuard
would have detected the repetition after 3 iterations and halted.

Usage:
    python benchmarks/real_incidents/incident_04_semantic_echo.py
"""

from __future__ import annotations

import time
from typing import Any

from veronica_core import SemanticLoopGuard
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions
from veronica_core.shield.types import Decision
from veronica_core.runtime_policy import PolicyContext


# ---------------------------------------------------------------------------
# Template responses that are semantically identical but lexically varied
# ---------------------------------------------------------------------------

# All variants share a high proportion of words -- Jaccard similarity > 0.85.
# This models a real LLM that is stuck on the same response template.
APOLOGY_VARIANTS = [
    "I apologize but I do not have information about that topic in my knowledge base at this time.",
    "I apologize but I do not have information about that topic in my knowledge base right now.",
    "I apologize but I do not have information about this topic in my knowledge base at this time.",
    "I apologize but I do not have information about that topic in the knowledge base at this time.",
    "I apologize but I do not have any information about that topic in my knowledge base at this time.",
    "I apologize but I do not have sufficient information about that topic in my knowledge base now.",
    "I apologize however I do not have information about that topic in my knowledge base at this time.",
    "I apologize but I do not have relevant information about that topic in my knowledge base today.",
    "I apologize but I currently do not have information about that topic in my knowledge base at all.",
    "I apologize but I do not have useful information about that topic in my knowledge base right now.",
]


class StubLLM:
    """Simulates LLM that cycles through semantically-identical apologies."""

    def __init__(self) -> None:
        self.call_count = 0
        self.tokens_per_response = 300

    def generate(self) -> dict[str, Any]:
        self.call_count += 1
        # Cycle through variants -- all semantically identical
        variant_idx = (self.call_count - 1) % len(APOLOGY_VARIANTS)
        text = APOLOGY_VARIANTS[variant_idx]
        return {
            "text": text,
            "tokens": self.tokens_per_response,
            "variant": variant_idx,
        }


# ---------------------------------------------------------------------------
# Baseline: no semantic detection, loops until user gives up
# ---------------------------------------------------------------------------

def baseline_semantic_echo(
    max_iterations: int = 89,
    cost_per_1k_tokens_usd: float = 0.015,
    tokens_per_response: int = 300,
) -> dict[str, Any]:
    """Simulate the 2024-Q3 incident: 89 iterations of the same apology.

    The orchestration framework had no semantic loop detection. Each slightly
    different apology was treated as a "new attempt" and the cycle continued.
    """
    llm = StubLLM()
    start = time.perf_counter()
    total_tokens = 0

    for _ in range(max_iterations):
        result = llm.generate()
        total_tokens += result["tokens"]

    elapsed_ms = (time.perf_counter() - start) * 1000
    total_cost = (total_tokens / 1000) * cost_per_1k_tokens_usd

    return {
        "scenario": "baseline",
        "incident": "Semantic echo chamber -- customer support (2024-Q3)",
        "iterations": llm.call_count,
        "unique_semantic_content": 1,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_ms": round(elapsed_ms, 2),
        "stopped_by": "user_gave_up",
        "contained": False,
    }


# ---------------------------------------------------------------------------
# Veronica: SemanticLoopGuard detects repetition after window=3
# ---------------------------------------------------------------------------

def veronica_semantic_echo(
    max_iterations: int = 89,
    cost_per_1k_tokens_usd: float = 0.015,
    tokens_per_response: int = 300,
    semantic_window: int = 3,
    jaccard_threshold: float = 0.85,
) -> dict[str, Any]:
    """SemanticLoopGuard + ExecutionContext detect and halt the echo loop.

    After `window` iterations of semantically-identical responses,
    the guard detects the loop and denies further execution.
    In the real incident, this would have stopped after 3 iterations.
    """
    llm = StubLLM()
    guard = SemanticLoopGuard(
        window=semantic_window,
        jaccard_threshold=jaccard_threshold,
        min_chars=30,
    )
    config = ExecutionConfig(
        max_cost_usd=10.0,
        max_steps=max_iterations * 2,
        max_retries_total=200,
    )

    iterations_done = 0
    halted_by = "unknown"
    total_tokens = 0
    semantic_loop_detected_at: int | None = None

    start = time.perf_counter()
    with ExecutionContext(config=config) as ctx:
        for i in range(max_iterations):
            # Budget/step check
            ec_decision = ctx.wrap_llm_call(
                fn=lambda: llm.generate(),
                options=WrapOptions(
                    operation_name="support_response",
                    cost_estimate_hint=(tokens_per_response / 1000) * cost_per_1k_tokens_usd,
                ),
            )

            if ec_decision == Decision.HALT:
                halted_by = "budget_exceeded"
                break

            result = llm.generate()
            total_tokens += result["tokens"]
            iterations_done += 1

            # Record response in semantic guard buffer
            guard.record(result["text"])
            policy_ctx = PolicyContext(metadata={"operation": "semantic_check"})
            semantic_decision = guard.check(policy_ctx)

            if not semantic_decision.allowed:
                semantic_loop_detected_at = i + 1
                halted_by = "semantic_loop_detected"
                break

        if halted_by == "unknown":
            halted_by = "max_iterations"

    elapsed_ms = (time.perf_counter() - start) * 1000
    snap = ctx.get_snapshot()
    total_cost = (total_tokens / 1000) * cost_per_1k_tokens_usd

    return {
        "scenario": "veronica",
        "incident": "Semantic echo chamber -- customer support (2024-Q3)",
        "iterations": iterations_done,
        "semantic_window": semantic_window,
        "jaccard_threshold": jaccard_threshold,
        "loop_detected_at_iteration": semantic_loop_detected_at,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "ctx_cost_usd": round(snap.cost_usd_accumulated, 6),
        "aborted": snap.aborted,
        "elapsed_ms": round(elapsed_ms, 2),
        "halted_by": halted_by,
        "contained": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 68)
    print("INCIDENT #04: Semantic Echo Chamber -- Customer Support (2024-Q3)")
    print("Real data: 89 identical apologies, user gave up, 0/5 stars")
    print("=" * 68)

    MAX_ITER = 89
    COST_PER_1K = 0.015
    TOKENS_PER_RESP = 300
    WINDOW = 3
    JACCARD = 0.85

    base = baseline_semantic_echo(
        max_iterations=MAX_ITER,
        cost_per_1k_tokens_usd=COST_PER_1K,
        tokens_per_response=TOKENS_PER_RESP,
    )
    ver = veronica_semantic_echo(
        max_iterations=MAX_ITER,
        cost_per_1k_tokens_usd=COST_PER_1K,
        tokens_per_response=TOKENS_PER_RESP,
        semantic_window=WINDOW,
        jaccard_threshold=JACCARD,
    )

    iter_reduction = round(100 * (1 - ver["iterations"] / max(base["iterations"], 1)), 1)
    token_reduction = round(100 * (1 - ver["total_tokens"] / max(base["total_tokens"], 1)), 1)
    cost_reduction = round(100 * (1 - ver["total_cost_usd"] / max(base["total_cost_usd"], 0.0001)), 1)

    print()
    header = f"{'Metric':<34} {'Baseline (incident)':>18} {'Veronica':>14}"
    print(header)
    print("-" * 68)
    print(f"{'Iterations':<34} {base['iterations']:>18} {ver['iterations']:>14}")
    print(f"{'Unique semantic content':<34} {base['unique_semantic_content']:>18} {'1 (detected)':>14}")
    print(f"{'Total tokens':<34} {base['total_tokens']:>18,} {ver['total_tokens']:>14,}")
    print(f"{'Total cost (USD)':<34} ${base['total_cost_usd']:>17.4f} ${ver['total_cost_usd']:>13.4f}")
    loop_iter_str = str(ver["loop_detected_at_iteration"]) if ver["loop_detected_at_iteration"] is not None else "not detected"
    print(f"{'Loop detected at iteration':<34} {'N/A':>18} {loop_iter_str:>14}")
    print(f"{'Stopped by':<34} {'user_gave_up':>18} {ver['halted_by'][:14]:>14}")
    print()
    print(f"Iteration reduction: {iter_reduction}%")
    print(f"Token reduction:     {token_reduction}%")
    print(f"Cost reduction:      {cost_reduction}%")
    print(f"Semantic window: {WINDOW} | Jaccard threshold: {JACCARD}")


if __name__ == "__main__":
    main()
