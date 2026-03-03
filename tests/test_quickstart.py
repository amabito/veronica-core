"""Tests for veronica_core.quickstart -- init()/shutdown()/_parse_budget().

Covers happy-path, edge cases, and adversarial scenarios including
concurrency, corrupted input, state-corruption patterns, and on_halt
dispatch behaviour (raise/warn/silent).
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import patch

import pytest

from veronica_core.containment.execution_context import ExecutionContext
from veronica_core.inject import VeronicaHalt
from veronica_core.quickstart import _parse_budget, get_context, init, shutdown
from veronica_core.shield.types import Decision


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
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("$5.00", 5.0, id="dollar_with_cents"),
            pytest.param("5.0", 5.0, id="float_no_dollar"),
            pytest.param("10", 10.0, id="integer_string"),
            pytest.param("$0.01", 0.01, id="tiny_value"),
            pytest.param("$10000", 10000.0, id="large_value"),
            pytest.param("  $5.00  ", 5.0, id="outer_whitespace_stripped"),
            pytest.param("$ 5.00", 5.0, id="internal_whitespace_after_dollar"),
        ],
    )
    def test_valid_budget_parsed(self, raw: str, expected: float) -> None:
        assert _parse_budget(raw) == pytest.approx(expected)


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

    @pytest.mark.parametrize(
        "bad_input",
        [
            pytest.param("", id="empty_string"),
            pytest.param("$", id="dollar_only"),
            pytest.param("-$5", id="negative_value"),
            pytest.param("$0", id="zero_value"),
            pytest.param("nan", id="nan_string"),
            pytest.param("inf", id="inf_string"),
            pytest.param("-inf", id="negative_inf_string"),
            pytest.param("five dollars", id="word"),
            pytest.param("EUR5", id="currency_prefix_eur"),
            pytest.param("5abc", id="letters_mixed"),
        ],
    )
    def test_invalid_budget_raises_value_error(self, bad_input: str) -> None:
        with pytest.raises(ValueError):
            _parse_budget(bad_input)

    def test_double_dollar_sign_parses_as_valid(self) -> None:
        # "$$5" -> lstrip("$") removes both $ -> "5" -> 5.0 is valid (not an error).
        result = _parse_budget("$$5")
        assert result == pytest.approx(5.0)

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


# ---------------------------------------------------------------------------
# TestOnHaltDispatch -- behavioural tests for on_halt=raise/warn/silent
# ---------------------------------------------------------------------------


class TestOnHaltDispatch:
    """Verify that on_halt actually dispatches HALT decisions correctly."""

    def _make_halting_ctx(self, mode: str) -> ExecutionContext:
        """Init with given on_halt mode and monkey-patch wrap_llm_call to return HALT."""
        ctx = init("$100.00", on_halt=mode)  # type: ignore[arg-type]
        # Replace wrap_llm_call with a fake that always returns HALT,
        # then re-install the on_halt dispatcher on top.
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: Decision.HALT  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, mode)
        return ctx

    def test_on_halt_raise_raises_veronica_halt(self) -> None:
        ctx = self._make_halting_ctx("raise")
        with pytest.raises(VeronicaHalt, match="HALT"):
            ctx.wrap_llm_call()

    def test_on_halt_warn_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = self._make_halting_ctx("warn")
        with caplog.at_level(logging.WARNING, logger="veronica_core.quickstart"):
            result = ctx.wrap_llm_call()
        assert result == Decision.HALT
        assert any("HALT" in record.message for record in caplog.records)

    def test_on_halt_silent_returns_decision_unchanged(self) -> None:
        ctx = init("$100.00", on_halt="silent")
        # For silent mode, wrap_llm_call is NOT patched, so we mock the
        # original directly to return HALT.
        ctx.wrap_llm_call = lambda *a, **kw: Decision.HALT  # type: ignore[method-assign]
        result = ctx.wrap_llm_call()
        assert result == Decision.HALT
        # No exception, no log -- just the raw Decision.

    def test_on_halt_raise_does_not_trigger_on_allow(self) -> None:
        ctx = init("$100.00", on_halt="raise")
        # Replace underlying with ALLOW, then re-install dispatcher.
        ctx.wrap_llm_call = lambda *a, **kw: Decision.ALLOW  # type: ignore[method-assign]
        from veronica_core.quickstart import _install_on_halt_dispatch
        _install_on_halt_dispatch(ctx, "raise")
        result = ctx.wrap_llm_call()
        assert result == Decision.ALLOW  # no exception


# ---------------------------------------------------------------------------
# TestAdversarialOnHalt -- attacker mindset for on_halt dispatch
# ---------------------------------------------------------------------------


class TestAdversarialOnHalt:
    """Adversarial tests for on_halt dispatch -- how to break it."""

    def test_non_decision_return_passes_through_raise_mode(self) -> None:
        """If wrap_llm_call returns a non-Decision (e.g. raw string), no crash."""
        ctx = init("$100.00", on_halt="raise")
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: "NOT_A_DECISION"  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, "raise")
        result = ctx.wrap_llm_call()
        assert result == "NOT_A_DECISION"

    def test_none_return_passes_through(self) -> None:
        """wrap_llm_call returning None must not crash dispatcher."""
        ctx = init("$100.00", on_halt="raise")
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: None  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, "raise")
        result = ctx.wrap_llm_call()
        assert result is None

    def test_original_raises_exception_propagates(self) -> None:
        """If the underlying wrap_llm_call raises, the exception propagates."""
        ctx = init("$100.00", on_halt="raise")
        from veronica_core.quickstart import _install_on_halt_dispatch

        def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("boom")

        ctx.wrap_llm_call = _boom  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, "raise")
        with pytest.raises(RuntimeError, match="boom"):
            ctx.wrap_llm_call()

    def test_concurrent_halt_raise_all_get_exception(self) -> None:
        """Multiple threads hitting HALT in raise mode all get VeronicaHalt."""
        ctx = init("$100.00", on_halt="raise")
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: Decision.HALT  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, "raise")

        exceptions: list[BaseException] = []
        barrier = threading.Barrier(5)

        def call() -> None:
            barrier.wait()
            try:
                ctx.wrap_llm_call()
            except VeronicaHalt as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(exceptions) == 5
        assert all(isinstance(e, VeronicaHalt) for e in exceptions)

    def test_all_decision_variants_no_crash(self) -> None:
        """ALLOW, RETRY, DEGRADE, QUARANTINE, QUEUE must not raise in raise mode."""
        for decision in [Decision.ALLOW, Decision.RETRY, Decision.DEGRADE,
                         Decision.QUARANTINE, Decision.QUEUE]:
            shutdown()
            ctx = init("$100.00", on_halt="raise")
            from veronica_core.quickstart import _install_on_halt_dispatch
            ctx.wrap_llm_call = lambda *a, d=decision, **kw: d  # type: ignore[method-assign]
            _install_on_halt_dispatch(ctx, "raise")
            result = ctx.wrap_llm_call()
            assert result == decision

    def test_shutdown_after_halt_cleans_up(self) -> None:
        """After on_halt=raise triggers VeronicaHalt, shutdown() is still safe."""
        ctx = init("$100.00", on_halt="raise")
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: Decision.HALT  # type: ignore[method-assign]
        _install_on_halt_dispatch(ctx, "raise")
        with pytest.raises(VeronicaHalt):
            ctx.wrap_llm_call()
        shutdown()
        assert get_context() is None

    def test_double_install_warn_logs_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Double-calling _install_on_halt_dispatch must NOT stack wrappers.

        If stacked, warn mode would log twice per HALT.  This test verifies
        exactly one warning is emitted.
        """
        ctx = init("$100.00", on_halt="warn")
        from veronica_core.quickstart import _install_on_halt_dispatch
        ctx.wrap_llm_call = lambda *a, **kw: Decision.HALT  # type: ignore[method-assign]
        # Install twice -- the second call must NOT stack on top of the first.
        _install_on_halt_dispatch(ctx, "warn")
        _install_on_halt_dispatch(ctx, "warn")
        with caplog.at_level(logging.WARNING, logger="veronica_core.quickstart"):
            ctx.wrap_llm_call()
        halt_records = [r for r in caplog.records if "HALT" in r.message]
        assert len(halt_records) == 1, (
            f"Expected exactly 1 HALT warning but got {len(halt_records)}; "
            f"wrapper stacking detected"
        )

    def test_init_shutdown_interleaving_storm(self) -> None:
        """Rapid init/shutdown cycling must not corrupt module state."""
        for _ in range(20):
            ctx = init("$1.00", on_halt="silent")
            assert get_context() is ctx
            shutdown()
            assert get_context() is None


# ---------------------------------------------------------------------------
# TestAdversarialRedactExc -- attacker mindset for credential redaction
# ---------------------------------------------------------------------------


class TestAdversarialRedactExc:
    """Adversarial tests for _redact_exc in distributed.py."""

    def test_multiple_urls_in_single_message(self) -> None:
        """Multiple redis URLs in one message must all be redacted."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError(
            "primary redis://admin:pass1@host1:6379/0 "
            "replica redis://reader:pass2@host2:6380/1"
        )
        result = _redact_exc(exc)
        assert "pass1" not in result
        assert "pass2" not in result
        assert "admin" not in result
        assert "reader" not in result

    def test_url_with_special_chars_in_password(self) -> None:
        """Passwords with @, :, / must still be fully redacted."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError("redis://user:p%40ss:w/rd@host:6379/0")
        result = _redact_exc(exc)
        assert "p%40ss" not in result
        assert "***@host" in result

    def test_empty_exception_message(self) -> None:
        from veronica_core.distributed import _redact_exc

        exc = RuntimeError("")
        result = _redact_exc(exc)
        assert result == "RuntimeError: "

    def test_url_without_credentials_unchanged(self) -> None:
        """redis://host:6379/0 (no user:pass) must pass through unchanged."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError("redis://host:6379/0 refused")
        result = _redact_exc(exc)
        assert "redis://host:6379/0" in result

    def test_uppercase_scheme_redacted(self) -> None:
        """REDIS:// (uppercase) must be redacted -- case-insensitive."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError("REDIS://admin:secret@host:6379/0")
        result = _redact_exc(exc)
        assert "secret" not in result
        assert "admin" not in result

    def test_redis_plus_ssl_scheme_redacted(self) -> None:
        """redis+ssl:// scheme must also be redacted."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError("redis+ssl://user:pass@secure.host:6380/1")
        result = _redact_exc(exc)
        assert "user:" not in result
        assert "***@secure.host" in result

    def test_password_with_literal_at_sign(self) -> None:
        """Password containing literal '@' must be fully redacted (no partial leak)."""
        from veronica_core.distributed import _redact_exc

        exc = ConnectionError("redis://user:p@ss@host:6379/0")
        result = _redact_exc(exc)
        assert "p@ss" not in result
        assert "***@host:6379/0" in result
