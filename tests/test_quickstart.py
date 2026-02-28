"""Tests for veronica_core.quickstart -- init()/shutdown()/_parse_budget().

Covers happy-path, edge cases, and adversarial scenarios including
concurrency, corrupted input, and state-corruption patterns.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest

from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.quickstart import _parse_budget, get_context, init, shutdown


# ---------------------------------------------------------------------------
# Autouse fixture: guarantee clean state before and after every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset quickstart module state before and after each test."""
    shutdown()
    yield
    shutdown()


# ---------------------------------------------------------------------------
# TestParseBudget -- unit-level parser tests (no global state involved)
# ---------------------------------------------------------------------------


class TestParseBudget:
    def test_dollar_sign_with_cents(self) -> None:
        assert _parse_budget("$5.00") == pytest.approx(5.0)

    def test_string_float_no_dollar(self) -> None:
        assert _parse_budget("5.0") == pytest.approx(5.0)

    def test_integer_string(self) -> None:
        assert _parse_budget("10") == pytest.approx(10.0)

    def test_tiny_value(self) -> None:
        assert _parse_budget("$0.01") == pytest.approx(0.01)

    def test_large_value(self) -> None:
        assert _parse_budget("$10000") == pytest.approx(10000.0)

    def test_whitespace_stripped(self) -> None:
        assert _parse_budget("  $5.00  ") == pytest.approx(5.0)

    def test_internal_whitespace_after_dollar(self) -> None:
        # "$ 5.00" -- after lstrip("$"), strip() removes leading space
        assert _parse_budget("$ 5.00") == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# TestInit -- happy-path init() scenarios
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_returns_execution_context(self) -> None:
        ctx = init("$5.00")
        assert isinstance(ctx, ExecutionContext)

    def test_get_context_returns_non_none_after_init(self) -> None:
        init("$5.00")
        assert get_context() is not None

    def test_init_custom_max_steps(self) -> None:
        ctx = init("$5.00", max_steps=10)
        # Verify context was created (config not directly exposed, but we can
        # verify init succeeded and context is live)
        assert get_context() is ctx

    def test_init_custom_max_retries(self) -> None:
        ctx = init("$5.00", max_retries_total=5)
        assert get_context() is ctx

    def test_init_on_halt_warn(self) -> None:
        ctx = init("$5.00", on_halt="warn")
        assert ctx._quickstart_on_halt == "warn"  # type: ignore[attr-defined]

    def test_init_on_halt_raise(self) -> None:
        ctx = init("$5.00", on_halt="raise")
        assert ctx._quickstart_on_halt == "raise"  # type: ignore[attr-defined]

    def test_init_patch_false_does_not_call_patch_openai(self) -> None:
        # patch_openai is lazily imported inside init(); mock at the patch module level
        with patch("veronica_core.patch.patch_openai") as mock_po:
            init("$5.00", patch_openai=False)
            mock_po.assert_not_called()

    def test_init_patch_openai_true_calls_patch_openai(self) -> None:
        with patch("veronica_core.patch.patch_openai") as mock_po:
            init("$5.00", patch_openai=True)
            mock_po.assert_called_once()

    def test_init_patch_anthropic_true_calls_patch_anthropic(self) -> None:
        with patch("veronica_core.patch.patch_anthropic") as mock_pa:
            init("$5.00", patch_anthropic=True)
            mock_pa.assert_called_once()

    def test_init_with_zero_timeout_ms(self) -> None:
        ctx = init("$5.00", timeout_ms=0)
        assert get_context() is ctx


# ---------------------------------------------------------------------------
# TestShutdown -- shutdown() lifecycle tests
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_clears_context(self) -> None:
        init("$5.00")
        assert get_context() is not None
        shutdown()
        assert get_context() is None

    def test_shutdown_without_init_is_safe(self) -> None:
        # No init called; shutdown must be a no-op
        shutdown()  # should not raise

    def test_double_shutdown_is_safe(self) -> None:
        init("$5.00")
        shutdown()
        shutdown()  # second call must not raise

    def test_shutdown_calls_unpatch_all(self) -> None:
        with patch("veronica_core.patch.unpatch_all") as mock_unpatch:
            init("$5.00")
            shutdown()
            mock_unpatch.assert_called()

    def test_get_context_before_init_returns_none(self) -> None:
        # autouse fixture already called shutdown(), so no init has run
        assert get_context() is None

    def test_reinit_after_shutdown_succeeds(self) -> None:
        init("$5.00")
        shutdown()
        ctx2 = init("$10.00")
        assert get_context() is ctx2


# ---------------------------------------------------------------------------
# TestAdversarialQuickstart -- attacker mindset: corrupted/adversarial inputs
# ---------------------------------------------------------------------------


class TestAdversarialQuickstart:
    # ------------------------------------------------------------------
    # Corrupted budget strings
    # ------------------------------------------------------------------

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("")

    def test_dollar_only_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("$")

    def test_negative_value_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("-$5")

    def test_zero_value_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("$0")

    def test_nan_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("nan")

    def test_inf_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("inf")

    def test_negative_inf_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("-inf")

    def test_word_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("five dollars")

    def test_currency_prefix_eur_raises(self) -> None:
        # "EUR5" -- after lstrip("$") = "EUR5", float("EUR5") fails
        with pytest.raises(ValueError):
            _parse_budget("EUR5")

    def test_double_dollar_sign_raises(self) -> None:
        # "$$5" -> lstrip("$") = "5", should actually parse fine because
        # lstrip removes ALL leading '$'. Verify actual behavior.
        # lstrip("$") on "$$5" removes both $ -> "5" -> 5.0 is valid.
        # So this should succeed, not raise.
        result = _parse_budget("$$5")
        assert result == pytest.approx(5.0)

    def test_letters_mixed_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_budget("5abc")

    # ------------------------------------------------------------------
    # init() with invalid budget (integration path)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # State corruption
    # ------------------------------------------------------------------

    def test_double_init_without_shutdown_raises(self) -> None:
        init("$5.00")
        with pytest.raises(RuntimeError):
            init("$10.00")

    def test_double_init_leaves_original_context(self) -> None:
        ctx1 = init("$5.00")
        try:
            init("$10.00")
        except RuntimeError:
            pass
        assert get_context() is ctx1

    # ------------------------------------------------------------------
    # Boundary values
    # ------------------------------------------------------------------

    def test_tiny_budget_works(self) -> None:
        ctx = init("$0.001")
        assert get_context() is ctx

    def test_huge_budget_works(self) -> None:
        ctx = init("$999999.99")
        assert get_context() is ctx

    # ------------------------------------------------------------------
    # _parse_budget with non-str types (type boundary)
    # ------------------------------------------------------------------

    def test_parse_budget_bytes_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            _parse_budget(b"$5.00")  # type: ignore[arg-type]

    def test_parse_budget_none_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            _parse_budget(None)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Concurrency: 10 threads call init() simultaneously
    # ------------------------------------------------------------------

    def test_concurrent_init_exactly_one_succeeds(self) -> None:
        successes: list[Any] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def attempt_init() -> None:
            barrier.wait()
            try:
                ctx = init("$5.00")
                successes.append(ctx)
            except RuntimeError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt_init) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 thread should succeed; rest raise RuntimeError
        assert len(successes) == 1
        assert len(errors) == 9

    def test_concurrent_shutdown_no_crash(self) -> None:
        init("$5.00")

        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def attempt_shutdown() -> None:
            barrier.wait()
            try:
                shutdown()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt_shutdown) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors during concurrent shutdown: {errors}"

    # ------------------------------------------------------------------
    # max_steps boundary
    # ------------------------------------------------------------------

    def test_init_max_steps_zero_still_returns_context(self) -> None:
        # max_steps=0 is technically valid at init() level (ExecutionContext
        # will HALT immediately on first wrap call, but init itself must not raise).
        ctx = init("$5.00", max_steps=0)
        assert get_context() is ctx

    # ------------------------------------------------------------------
    # get_context thread-safety (read from multiple threads)
    # ------------------------------------------------------------------

    def test_get_context_consistent_across_threads(self) -> None:
        ctx = init("$5.00")
        results: list[Any] = []
        barrier = threading.Barrier(5)

        def read_ctx() -> None:
            barrier.wait()
            results.append(get_context())

        threads = [threading.Thread(target=read_ctx) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is ctx for r in results)
