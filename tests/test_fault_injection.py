"""Fault injection tests (S-6).

Verifies graceful handling of:
1. JSONBackend with corrupted JSON file — must not crash, must return None.
2. ShieldPipeline hook that raises an exception — pipeline must not silently swallow it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.backends import JSONBackend
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions


# ---------------------------------------------------------------------------
# JSONBackend fault injection
# ---------------------------------------------------------------------------


class TestJSONBackendFaultInjection:
    """JSONBackend must degrade gracefully when the state file is corrupted."""

    def test_corrupted_json_returns_none_not_crash(self, tmp_path: Path) -> None:
        """GIVEN a JSONBackend pointing to a file with invalid JSON,
        WHEN load() is called,
        THEN it returns None (graceful fallback) and does not raise an exception.
        """
        corrupt_path = tmp_path / "corrupt.json"
        corrupt_path.write_text("{this is not valid JSON!!!", encoding="utf-8")

        backend = JSONBackend(corrupt_path)
        result = backend.load()

        assert result is None, (
            f"Expected None from corrupted JSON file, got: {result!r}"
        )

    def test_truncated_json_returns_none(self, tmp_path: Path) -> None:
        """GIVEN a JSONBackend with a truncated (incomplete) JSON file,
        WHEN load() is called,
        THEN it returns None without crashing.
        """
        truncated_path = tmp_path / "truncated.json"
        truncated_path.write_text('{"key": "val', encoding="utf-8")

        backend = JSONBackend(truncated_path)
        result = backend.load()

        assert result is None, f"Expected None from truncated JSON, got: {result!r}"

    def test_empty_json_file_returns_none(self, tmp_path: Path) -> None:
        """GIVEN an empty state file,
        WHEN load() is called,
        THEN it returns None gracefully.
        """
        empty_path = tmp_path / "empty.json"
        empty_path.write_text("", encoding="utf-8")

        backend = JSONBackend(empty_path)
        result = backend.load()

        assert result is None, f"Expected None from empty file, got: {result!r}"

    def test_valid_json_after_corrupt_roundtrip(self, tmp_path: Path) -> None:
        """GIVEN a corrupted file that is then overwritten with valid data via save(),
        WHEN load() is called after save(),
        THEN the valid data is returned correctly.
        """
        state_path = tmp_path / "state.json"
        state_path.write_text("CORRUPTED", encoding="utf-8")

        backend = JSONBackend(state_path)
        # Load returns None (graceful fallback)
        assert backend.load() is None

        # After save(), load returns valid data
        data = {"cooldown_fails": 3, "fail_counts": {"task_a": 2}}
        backend.save(data)
        loaded = backend.load()

        assert loaded is not None
        assert loaded["cooldown_fails"] == 3

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        """GIVEN a JSONBackend pointing to a non-existent file,
        WHEN load() is called,
        THEN it returns None (no crash, no exception).
        """
        missing_path = tmp_path / "does_not_exist.json"
        backend = JSONBackend(missing_path)
        result = backend.load()
        assert result is None


# ---------------------------------------------------------------------------
# ShieldPipeline hook fault injection
# ---------------------------------------------------------------------------


class TestShieldPipelineHookFaultInjection:
    """ShieldPipeline must NOT silently swallow hook exceptions."""

    def test_pre_dispatch_hook_exception_propagates(self) -> None:
        """GIVEN a pre_dispatch hook that raises RuntimeError,
        WHEN before_llm_call() is invoked,
        THEN the RuntimeError propagates to the caller (not silently swallowed).
        """
        class BrokenPreDispatch:
            def before_llm_call(self, ctx) -> Decision:
                raise RuntimeError("pre_dispatch hook exploded")

        pipeline = ShieldPipeline(pre_dispatch=BrokenPreDispatch())
        from veronica_core.shield.types import ToolCallContext
        ctx = ToolCallContext(request_id="test-req", session_id="s1")

        with pytest.raises(RuntimeError, match="pre_dispatch hook exploded"):
            pipeline.before_llm_call(ctx)

    def test_tool_dispatch_hook_exception_propagates(self) -> None:
        """GIVEN a tool_dispatch hook that raises ValueError,
        WHEN before_tool_call() is invoked,
        THEN the ValueError propagates (pipeline does not silently absorb it).
        """
        class BrokenToolDispatch:
            def before_tool_call(self, ctx) -> Decision:
                raise ValueError("tool_dispatch hook failure")

        pipeline = ShieldPipeline(tool_dispatch=BrokenToolDispatch())
        from veronica_core.shield.types import ToolCallContext
        ctx = ToolCallContext(request_id="test-req2", session_id="s2")

        with pytest.raises(ValueError, match="tool_dispatch hook failure"):
            pipeline.before_tool_call(ctx)

    def test_execution_context_propagates_hook_exception(self) -> None:
        """GIVEN an ExecutionContext with a pipeline whose hook raises,
        WHEN wrap_llm_call() is called,
        THEN the exception propagates to the caller (not silently absorbed).

        This verifies that hook failures are not swallowed by the containment layer.
        """
        class ExplodingHook:
            def before_llm_call(self, ctx) -> Decision:
                raise RuntimeError("deliberate hook failure")

        pipeline = ShieldPipeline(pre_dispatch=ExplodingHook())
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
        ctx = ExecutionContext(config=config, pipeline=pipeline)

        with pytest.raises(RuntimeError, match="deliberate hook failure"):
            ctx.wrap_llm_call(fn=lambda: None)

    def test_budget_hook_exception_propagates(self) -> None:
        """GIVEN a budget hook that raises on before_charge(),
        WHEN a successful LLM call completes and charge is attempted,
        THEN the exception propagates rather than being swallowed.
        """
        class ExplodingBudgetHook:
            def before_charge(self, ctx, cost_usd: float) -> Decision:
                raise RuntimeError("budget hook exploded on charge")

        pipeline = ShieldPipeline(budget=ExplodingBudgetHook())
        config = ExecutionConfig(max_cost_usd=10.0, max_steps=50, max_retries_total=5)
        ctx = ExecutionContext(config=config, pipeline=pipeline)

        with pytest.raises(RuntimeError, match="budget hook exploded on charge"):
            ctx.wrap_llm_call(
                fn=lambda: None,
                options=WrapOptions(cost_estimate_hint=0.01),
            )
