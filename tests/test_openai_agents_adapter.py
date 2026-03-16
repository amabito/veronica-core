"""Tests for veronica_core.adapters.openai_agents (scaffold).

Coverage is limited to the implemented surface -- config dataclass and
adapter instantiation. Methods marked NOT IMPLEMENTED in the source
(wrap, on_agent_start, on_agent_step, on_tool_call) are not tested here;
they will be covered when the SDK integration is complete.
"""

from __future__ import annotations

from veronica_core.adapters.openai_agents import (
    OpenAIAgentsAdapter,
    OpenAIAgentsConfig,
)


# ---------------------------------------------------------------------------
# Import safety -- must not require openai-agents SDK
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_imports_without_openai_agents_sdk(self) -> None:
        """The module must load even if the openai-agents package is absent."""
        # If we reach this line, the import at module level succeeded.
        assert OpenAIAgentsAdapter is not None
        assert OpenAIAgentsConfig is not None


# ---------------------------------------------------------------------------
# OpenAIAgentsConfig defaults
# ---------------------------------------------------------------------------


class TestOpenAIAgentsConfig:
    def test_default_max_cost_usd(self) -> None:
        cfg = OpenAIAgentsConfig()
        assert cfg.max_cost_usd == 1.0

    def test_default_max_steps(self) -> None:
        cfg = OpenAIAgentsConfig()
        assert cfg.max_steps == 50

    def test_default_max_retries(self) -> None:
        cfg = OpenAIAgentsConfig()
        assert cfg.max_retries == 3

    def test_default_failure_threshold(self) -> None:
        cfg = OpenAIAgentsConfig()
        assert cfg.failure_threshold == 5

    def test_defaults_are_positive(self) -> None:
        cfg = OpenAIAgentsConfig()
        assert cfg.max_cost_usd > 0
        assert cfg.max_steps > 0
        assert cfg.max_retries >= 0
        assert cfg.failure_threshold > 0

    def test_custom_values_stored(self) -> None:
        cfg = OpenAIAgentsConfig(
            max_cost_usd=0.25,
            max_steps=10,
            max_retries=1,
            failure_threshold=2,
        )
        assert cfg.max_cost_usd == 0.25
        assert cfg.max_steps == 10
        assert cfg.max_retries == 1
        assert cfg.failure_threshold == 2


# ---------------------------------------------------------------------------
# OpenAIAgentsAdapter instantiation
# ---------------------------------------------------------------------------


class TestOpenAIAgentsAdapter:
    def test_instantiate_with_defaults(self) -> None:
        adapter = OpenAIAgentsAdapter()
        assert adapter.config.max_cost_usd == 1.0

    def test_instantiate_with_explicit_config(self) -> None:
        cfg = OpenAIAgentsConfig(max_cost_usd=0.50, max_steps=20)
        adapter = OpenAIAgentsAdapter(config=cfg)
        assert adapter.config is cfg

    def test_config_property_returns_same_object(self) -> None:
        cfg = OpenAIAgentsConfig(max_cost_usd=2.0)
        adapter = OpenAIAgentsAdapter(config=cfg)
        assert adapter.config is cfg

    def test_none_config_uses_defaults(self) -> None:
        adapter = OpenAIAgentsAdapter(config=None)
        assert isinstance(adapter.config, OpenAIAgentsConfig)
        assert adapter.config.max_steps == 50

    def test_multiple_instances_are_independent(self) -> None:
        a1 = OpenAIAgentsAdapter(config=OpenAIAgentsConfig(max_cost_usd=0.10))
        a2 = OpenAIAgentsAdapter(config=OpenAIAgentsConfig(max_cost_usd=5.00))
        assert a1.config.max_cost_usd != a2.config.max_cost_usd

    def test_wrap_not_implemented(self) -> None:
        """wrap() must not exist yet -- scaffold only."""
        adapter = OpenAIAgentsAdapter()
        assert not hasattr(adapter, "wrap"), (
            "wrap() is not yet implemented -- remove this test when it is"
        )

    def test_on_agent_start_not_implemented(self) -> None:
        adapter = OpenAIAgentsAdapter()
        assert not hasattr(adapter, "on_agent_start")

    def test_on_tool_call_not_implemented(self) -> None:
        adapter = OpenAIAgentsAdapter()
        assert not hasattr(adapter, "on_tool_call")
