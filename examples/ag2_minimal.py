"""AG2 minimal — Policy enforcement via VeronicaConversableAgent.

VeronicaConversableAgent is a drop-in replacement for autogen.ConversableAgent.
It enforces budget and step limits before every generate_reply() call.

Install:
    pip install veronica-core autogen-agentchat

This example uses a stub in place of a real autogen agent so no API key
is required.  Replace StubAgent with a real ConversableAgent in production.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from veronica_core import GuardConfig
from veronica_core.inject import VeronicaHalt


class _StubConversableAgent:
    """Minimal stand-in for autogen.ConversableAgent.

    Replace with:
        from autogen import ConversableAgent
        agent = VeronicaConversableAgent(
            "assistant",
            config=GuardConfig(max_cost_usd=1.0, max_steps=5),
            llm_config={"model": "gpt-4o-mini"},
        )
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self._call_count = 0

    def generate_reply(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        sender: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[Union[str, Dict[str, Any]]]:
        self._call_count += 1
        return f"[{self.name}] reply #{self._call_count}"


class _StubVeronicaAgent(_StubConversableAgent):
    """Stub that mimics VeronicaConversableAgent enforcement.

    In production, import from veronica_core.adapters.ag2:
        from veronica_core.adapters.ag2 import VeronicaConversableAgent
    """

    def __init__(
        self,
        name: str,
        config: Union[GuardConfig, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        # Build the AIContainer the same way the real adapter does
        from veronica_core.adapters._shared import build_adapter_container

        self._container = build_adapter_container(config, None)

    def generate_reply(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        sender: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[Union[str, Dict[str, Any]]]:
        # Pre-call: check budget and step count
        decision = self._container.check(cost_usd=0.0)
        if not decision.allowed:
            raise VeronicaHalt(decision.reason, decision)

        reply = super().generate_reply(messages=messages, sender=sender, **kwargs)

        # Post-call: increment step counter
        if reply is not None and self._container.step_guard is not None:
            self._container.step_guard.step()

        return reply

    @property
    def container(self):  # noqa: ANN201
        return self._container


def main() -> None:
    # 1. Create a policy-enforced agent (drop-in for ConversableAgent)
    config = GuardConfig(max_cost_usd=1.0, max_steps=3)

    # Production: from veronica_core.adapters.ag2 import VeronicaConversableAgent
    agent = _StubVeronicaAgent("assistant", config=config)

    messages = [{"role": "user", "content": "Hello"}]

    # 2. Call generate_reply as normal — limits enforced transparently
    for i in range(1, 6):
        print(f"Call {i}:")
        try:
            reply = agent.generate_reply(messages=messages)
            steps = agent.container.step_guard
            used = steps.current_step if steps else 0
            print(f"  OK: {reply}  (steps={used}/{config.max_steps})")
        except VeronicaHalt as exc:
            print(f"  HALTED: {exc}")
            break


if __name__ == "__main__":
    main()
