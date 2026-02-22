"""OpenAI SDK + VERONICA Core -- Transparent patch-based enforcement.

Demonstrates patch_openai() / patch_anthropic(): one-line activation that
transparently enforces veronica_guard limits on all subsequent SDK calls
without touching call sites.

Architecture:
    patch_openai()                        # activate once at startup
    @veronica_guard(max_cost_usd=0.50)    # declare limit on the function
    def my_agent_loop(): ...              # existing code unchanged

This demo injects a minimal stub into sys.modules so it runs without
an API key or the openai package installed.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

from veronica_core import patch_openai, unpatch_all, veronica_guard
from veronica_core.inject import VeronicaHalt


# ---------------------------------------------------------------------------
# Stub: inject a minimal openai module so patch_openai() has a target
# ---------------------------------------------------------------------------

def _install_openai_stub() -> types.ModuleType:
    """Register a minimal openai stub in sys.modules.

    Returns the Completions class so tests can reach it directly.
    """
    if "openai" in sys.modules and not isinstance(sys.modules["openai"], MagicMock):
        # Real openai installed -- use it
        import openai.resources.chat.completions as _real
        return _real.Completions  # type: ignore[return-value]

    # Build just enough module hierarchy for patch_openai() to find its target
    stub_openai = types.ModuleType("openai")
    stub_resources = types.ModuleType("openai.resources")
    stub_chat = types.ModuleType("openai.resources.chat")
    stub_completions_mod = types.ModuleType("openai.resources.chat.completions")

    class _StubCompletions:
        @staticmethod
        def create(*args: Any, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "[stub response]"
            resp.usage = MagicMock()
            resp.usage.total_tokens = 300
            return resp

    stub_completions_mod.Completions = _StubCompletions  # type: ignore[attr-defined]
    stub_openai.resources = stub_resources  # type: ignore[attr-defined]
    stub_resources.chat = stub_chat  # type: ignore[attr-defined]
    stub_chat.completions = stub_completions_mod  # type: ignore[attr-defined]

    sys.modules["openai"] = stub_openai
    sys.modules["openai.resources"] = stub_resources
    sys.modules["openai.resources.chat"] = stub_chat
    sys.modules["openai.resources.chat.completions"] = stub_completions_mod

    return _StubCompletions


def _make_response(content: str, total_tokens: int = 300) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock()
    resp.usage.total_tokens = total_tokens
    return resp


# ---------------------------------------------------------------------------
# Demo 1: patch enforces limits inside @veronica_guard boundary
# ---------------------------------------------------------------------------

def demo_transparent_enforcement(Completions: Any) -> None:
    print("=" * 60)
    print("Demo 1: Transparent Patch Enforcement")
    print("=" * 60)
    print("patch_openai() active. Guard limit: $0.0005 (1 call).\n")

    patch_openai()

    call_count = 0

    @veronica_guard(max_cost_usd=0.0005, max_steps=100)
    def agent_step(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        fake_resp = _make_response(f"answer {call_count}", total_tokens=300)
        stub_client = MagicMock()
        # Call via the (now-patched) class method
        result = Completions.create(stub_client, model="gpt-4o-mini", messages=[])
        # Patch intercepts the call; return the fake result content
        return fake_resp.choices[0].message.content

    prompts = ["First question", "Second question (blocked by budget)"]
    for prompt in prompts:
        print(f"Calling: {prompt}")
        try:
            answer = agent_step(prompt)
            print(f"  OK: {answer}")
        except VeronicaHalt as e:
            print(f"  HALTED by veronica_guard: {e}")

    unpatch_all()
    print("\nunpatch_all() called -- SDK restored to original state.")


# ---------------------------------------------------------------------------
# Demo 2: calls outside a guard boundary pass through unmodified
# ---------------------------------------------------------------------------

def demo_passthrough_outside_guard(Completions: Any) -> None:
    print("\n" + "=" * 60)
    print("Demo 2: Calls Outside Guard Pass Through")
    print("=" * 60)

    patch_openai()

    stub_client = MagicMock()
    # No @veronica_guard here -> patch is a no-op, call goes straight through
    Completions.create(stub_client, model="gpt-4o-mini", messages=[])
    print("Called SDK outside @veronica_guard boundary.")
    print("  is_guard_active() == False -> patch is a no-op here.")
    print("  No VeronicaHalt is possible -- guard context required.")

    unpatch_all()


# ---------------------------------------------------------------------------
# Demo 3: idempotency -- double patch_openai() is safe
# ---------------------------------------------------------------------------

def demo_idempotency() -> None:
    print("\n" + "=" * 60)
    print("Demo 3: Idempotency")
    print("=" * 60)

    patch_openai()
    patch_openai()  # second call is a no-op
    patch_openai()  # third call is a no-op

    print("Called patch_openai() three times.")
    print("  No duplicate wrapping -- each patch key is registered once.")

    unpatch_all()
    unpatch_all()  # safe to call when nothing is patched
    print("  unpatch_all() is also idempotent.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nVERONICA Core -- OpenAI SDK Patch Showcase")
    print("One-line activation. Zero call-site changes.\n")

    Completions = _install_openai_stub()

    demo_transparent_enforcement(Completions)
    demo_passthrough_outside_guard(Completions)
    demo_idempotency()

    print("\n" + "=" * 60)
    print("Key Takeaway:")
    print("  patch_openai()  # once at startup")
    print("  @veronica_guard(max_cost_usd=1.0, max_steps=50)")
    print("  def agent(): openai.chat.completions.create(...)  # unchanged")
    print("=" * 60)


if __name__ == "__main__":
    main()
