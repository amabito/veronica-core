"""@veronica_guard decorator — Execution boundary injection for any callable.

The simplest integration path: add one decorator and VERONICA enforces
budget, step count, and retry limits on every call — no external framework
required.

Covered scenarios:
  1. Basic guard — limits enforced across multiple calls
  2. Nested guards — each function has its own independent boundary
  3. Introspection — read live spend / step metrics via _container
  4. Graceful denial — return_decision=True avoids exception-based flow
"""

from __future__ import annotations

from typing import Any


from veronica_core import veronica_guard
from veronica_core.inject import VeronicaHalt


# ---------------------------------------------------------------------------
# Simulate an LLM call (no API key needed)
# ---------------------------------------------------------------------------

_call_log: list[str] = []


def _fake_llm(prompt: str, *, tokens: int = 400) -> str:
    """Stub that records calls and returns a canned response."""
    _call_log.append(prompt)
    return f"[LLM #{len(_call_log)}] {prompt[:30]}..."


def _fake_spend(container: Any, tokens: int) -> None:
    """Record token cost manually (in real usage, patch_openai does this)."""
    if container.budget is not None:
        container.budget.spend(tokens * 0.000002)
    if container.step_guard is not None:
        container.step_guard.step()


# ---------------------------------------------------------------------------
# Demo 1: Basic guard — three calls, then halted on fourth
# ---------------------------------------------------------------------------

def demo_basic_guard() -> None:
    print("=" * 60)
    print("Demo 1: Basic Guard (max_steps=3)")
    print("=" * 60)

    @veronica_guard(max_cost_usd=10.0, max_steps=3)
    def ask(prompt: str) -> str:
        result = _fake_llm(prompt)
        _fake_spend(ask._container, tokens=400)
        return result

    questions = [
        "What is VERONICA?",
        "Why use a containment layer?",
        "What is a BudgetWindowHook?",
        "Are there examples?",  # step 4 — blocked
    ]

    for q in questions:
        print(f"\nask({q!r})")
        try:
            print(f"  -> {ask(q)}")
        except VeronicaHalt as e:
            print(f"  HALTED: {e}")


# ---------------------------------------------------------------------------
# Demo 2: Nested guards — independent limits per function
# ---------------------------------------------------------------------------

def demo_nested_guards() -> None:
    print("\n" + "=" * 60)
    print("Demo 2: Nested Guards (independent boundaries)")
    print("=" * 60)

    @veronica_guard(max_cost_usd=0.01, max_steps=2)
    def summarize(text: str) -> str:
        result = _fake_llm(f"Summarize: {text}")
        _fake_spend(summarize._container, tokens=200)
        return result

    @veronica_guard(max_cost_usd=5.0, max_steps=10)
    def classify(text: str) -> str:
        result = _fake_llm(f"Classify: {text}")
        _fake_spend(classify._container, tokens=100)
        return result

    texts = ["VERONICA enforces LLM limits.", "Budget exceeded triggers VeronicaHalt."]

    for text in texts:
        print(f"\nInput: {text[:40]}")
        try:
            s = summarize(text)
            print(f"  summarize -> {s}")
        except VeronicaHalt as e:
            print(f"  summarize HALTED: {e}")

        try:
            c = classify(text)
            print(f"  classify  -> {c}")
        except VeronicaHalt as e:
            print(f"  classify  HALTED: {e}")

    # summarize hits step=2 first; classify still has room
    print("\n[summarize exhausted its 2-step budget; classify continues independently]")


# ---------------------------------------------------------------------------
# Demo 3: Introspection via _container
# ---------------------------------------------------------------------------

def demo_introspection() -> None:
    print("\n" + "=" * 60)
    print("Demo 3: Live Introspection via _container")
    print("=" * 60)

    @veronica_guard(max_cost_usd=1.0, max_steps=10)
    def agent(prompt: str) -> str:
        result = _fake_llm(prompt)
        _fake_spend(agent._container, tokens=500)
        return result

    for i in range(3):
        agent(f"Step {i + 1}")

    c = agent._container
    steps = c.step_guard.current_step if c.step_guard else "N/A"
    spent = c.budget.spent_usd if c.budget else 0.0

    print(f"After 3 calls:")
    print(f"  Steps used:   {steps} / 10")
    print(f"  Budget spent: ${spent:.6f} / $1.00")
    print(f"  Container id: {id(c)} (same instance across calls)")


# ---------------------------------------------------------------------------
# Demo 4: Graceful denial with return_decision=True
# ---------------------------------------------------------------------------

def demo_graceful_denial() -> None:
    print("\n" + "=" * 60)
    print("Demo 4: Graceful Denial (return_decision=True)")
    print("=" * 60)

    @veronica_guard(max_cost_usd=10.0, max_steps=1, return_decision=True)
    def single_shot(prompt: str) -> Any:
        result = _fake_llm(prompt)
        _fake_spend(single_shot._container, tokens=300)
        return result

    for i in range(3):
        outcome = single_shot(f"Query {i + 1}")
        if hasattr(outcome, "allowed"):
            # PolicyDecision returned instead of raising
            print(f"  Query {i + 1}: DENIED -- {outcome.reason}")
        else:
            print(f"  Query {i + 1}: OK -- {outcome}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nVERONICA Core -- @veronica_guard Decorator Showcase")
    print("Add one decorator. Get automatic budget + step enforcement.\n")

    demo_basic_guard()
    demo_nested_guards()
    demo_introspection()
    demo_graceful_denial()

    print("\n" + "=" * 60)
    print("Key Takeaway:")
    print("  @veronica_guard(max_cost_usd=1.0, max_steps=20)")
    print("  def my_agent(prompt: str) -> str:")
    print("      return llm.complete(prompt)  # existing code unchanged")
    print("  -> Raises VeronicaHalt when limits are exceeded.")
    print("=" * 60)


if __name__ == "__main__":
    main()
