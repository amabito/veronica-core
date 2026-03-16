"""OpenAI Agents SDK adapter for VERONICA-Core.

Wraps OpenAI Agent execution with ExecutionContext containment.
Budget, step, retry, and circuit-breaker limits are enforced
per agent run.

Status: SCAFFOLD -- requires openai-agents SDK for full integration.
The adapter interface is defined here; wiring to the SDK's lifecycle hooks
is pending until the SDK stabilizes its extension API.

Requires: pip install openai-agents  (not yet a declared dependency)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class OpenAIAgentsConfig:
    """Configuration for OpenAI Agents SDK containment.

    All limits are applied per agent run (single call to wrap()).
    """

    max_cost_usd: float = 1.0
    """Hard budget ceiling per run in USD."""

    max_steps: int = 50
    """Maximum number of tool calls / agent steps per run."""

    max_retries: int = 3
    """Maximum number of retries on transient errors."""

    failure_threshold: int = 5
    """Number of consecutive failures before the circuit breaker opens."""

    # NOT IMPLEMENTED: tool_pinning, authority_level


class OpenAIAgentsAdapter:
    """Adapter for the OpenAI Agents SDK with VERONICA-Core containment.

    Status: SCAFFOLD. The public interface is defined. SDK integration is
    pending a stable lifecycle-hook API from the openai-agents package.

    Usage pattern (when SDK integration is complete):

        adapter = OpenAIAgentsAdapter(config=OpenAIAgentsConfig(max_cost_usd=0.50))
        # adapter.wrap(agent) adds containment hooks to the agent's run loop
    """

    def __init__(self, config: OpenAIAgentsConfig | None = None) -> None:
        self._config = config or OpenAIAgentsConfig()
        logger.info(
            "[OpenAIAgentsAdapter] initialized (scaffold -- SDK wiring pending)"
        )

    @property
    def config(self) -> OpenAIAgentsConfig:
        """Return the active containment configuration."""
        return self._config

    # NOT IMPLEMENTED: wrap(), on_agent_start(), on_agent_step(), on_tool_call()
    #
    # These will be implemented once the openai-agents SDK exposes stable
    # lifecycle hooks for external middleware. The planned integration points:
    #
    #   on_agent_start(agent, input) -> None
    #       Open an ExecutionContext; attach budget and circuit-breaker.
    #
    #   on_agent_step(agent, step_result) -> None
    #       Record cost; check step limit; raise HaltError on budget exceeded.
    #
    #   on_tool_call(tool_name, tool_args) -> None
    #       Verify tool schema against ToolPinRegistry if pinning is enabled.
    #
    #   wrap(agent) -> agent
    #       Return the agent with containment hooks attached.


__all__ = ["OpenAIAgentsConfig", "OpenAIAgentsAdapter"]
