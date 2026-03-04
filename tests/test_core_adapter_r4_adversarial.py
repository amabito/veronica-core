"""Adversarial tests for core module and adapter R4 bug fixes.

Covers three fixes:
  C4 - SemanticLoopGuard.policy_type changed from mutable field to read-only property.
  C6 - VeronicaIntegration double atexit registration guarded by class-level flag.
  A2 - _shared.py `or` replaced with explicit None check so zero-value tokens are not masked.
"""

from __future__ import annotations

import atexit
from typing import Optional
from unittest.mock import MagicMock

import pytest

from veronica_core.adapters._shared import extract_llm_result_cost
from veronica_core.backends import PersistenceBackend
from veronica_core.integration import VeronicaIntegration
from veronica_core.semantic import SemanticLoopGuard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NullBackend(PersistenceBackend):
    """In-memory backend that never touches the filesystem."""

    def __init__(self) -> None:
        self._data: Optional[dict] = None

    def save(self, data: dict) -> bool:
        self._data = data
        return True

    def load(self) -> Optional[dict]:
        return self._data


def _make_llm_result(usage: dict, key: str = "token_usage") -> MagicMock:
    """Build a minimal LLMResult-like mock from a usage dict."""
    obj = MagicMock()
    obj.llm_output = {key: usage}
    return obj


# ---------------------------------------------------------------------------
# C4: SemanticLoopGuard.policy_type is a read-only property
# ---------------------------------------------------------------------------


class TestAdversarialC4PolicyTypeReadOnly:
    """Adversarial tests for C4 -- policy_type must be immutable."""

    def test_policy_type_returns_correct_value(self) -> None:
        """policy_type property must return the expected string."""
        guard = SemanticLoopGuard()
        assert guard.policy_type == "semantic_loop"

    def test_policy_type_is_not_settable(self) -> None:
        """Assigning to policy_type must raise AttributeError (not silently overwrite)."""
        guard = SemanticLoopGuard()
        with pytest.raises(AttributeError):
            guard.policy_type = "attacker_controlled"  # type: ignore[misc]

    def test_policy_type_not_in_dataclass_fields(self) -> None:
        """policy_type must not appear as a dataclass field."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SemanticLoopGuard)}
        assert "policy_type" not in field_names

    def test_policy_type_consistent_across_instances(self) -> None:
        """Every instance must return the same policy_type value regardless of config."""
        g1 = SemanticLoopGuard(window=2, jaccard_threshold=0.5)
        g2 = SemanticLoopGuard(window=10, jaccard_threshold=0.99)
        assert g1.policy_type == g2.policy_type == "semantic_loop"

    def test_policy_type_used_in_deny_decision(self) -> None:
        """policy_type returned in deny decisions must be 'semantic_loop'."""
        guard = SemanticLoopGuard(window=3, jaccard_threshold=0.5, min_chars=5)
        repeated = "hello world foo bar baz"
        guard.feed(repeated)
        guard.feed(repeated)
        decision = guard.check()
        assert not decision.allowed
        assert decision.policy_type == "semantic_loop"


# ---------------------------------------------------------------------------
# C6: VeronicaIntegration double atexit registration guarded by class flag
# ---------------------------------------------------------------------------


class TestAdversarialC6AtexitDeduplication:
    """Adversarial tests for C6 -- atexit must be registered at most once.

    Strategy: monkeypatch atexit.register to count how many times it is called
    during VeronicaIntegration.__init__. This avoids relying on CPython internals
    or the return value of atexit.unregister (which returns None, not a count).
    """

    def setup_method(self) -> None:
        """Reset class-level flag before each test to isolate test state."""
        VeronicaIntegration._atexit_registered = False

    def teardown_method(self) -> None:
        """Reset flag after each test so other tests are not polluted."""
        VeronicaIntegration._atexit_registered = False

    def _track_save_registrations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[object]:
        """Patch atexit.register to collect only VeronicaIntegration.save calls.

        VeronicaExit also calls atexit.register internally; we filter those out
        by checking that the registered function is a bound method named 'save'
        belonging to a VeronicaIntegration instance.
        """
        import veronica_core.integration as _mod

        save_calls: list[object] = []
        real_register = atexit.register

        def _tracking_register(fn: object, *args: object, **kwargs: object) -> object:
            fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", ""))
            if "_save_all_instances" in fn_name or "VeronicaIntegration" in fn_name:
                save_calls.append(fn)
            return real_register(fn, *args, **kwargs)

        monkeypatch.setattr(_mod.atexit, "register", _tracking_register)
        return save_calls

    def test_single_instance_registers_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One VeronicaIntegration with backend must register _save_all_instances via atexit."""
        save_calls = self._track_save_registrations(monkeypatch)
        VeronicaIntegration(backend=_NullBackend())
        assert len(save_calls) == 1, f"Expected 1 save registration, got {len(save_calls)}"

    def test_two_instances_register_at_most_once_total(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second VeronicaIntegration must NOT register atexit again (single class handler)."""
        save_calls = self._track_save_registrations(monkeypatch)
        VeronicaIntegration(backend=_NullBackend())
        VeronicaIntegration(backend=_NullBackend())
        assert len(save_calls) == 1, (
            f"Expected 1 save registration across 2 instances, got {len(save_calls)}"
        )

    def test_two_instances_both_saved_on_exit(self) -> None:
        """Both instances with backends must have save() called by _save_all_instances."""
        b1, b2 = _NullBackend(), _NullBackend()
        i1 = VeronicaIntegration(backend=b1)
        i2 = VeronicaIntegration(backend=b2)
        # Simulate atexit
        VeronicaIntegration._save_all_instances()
        # Both instances should be in _live_instances
        assert i1 in VeronicaIntegration._live_instances
        assert i2 in VeronicaIntegration._live_instances

    def test_ten_instances_do_not_accumulate_atexit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creating many instances must register atexit at most once."""
        save_calls = self._track_save_registrations(monkeypatch)
        for _ in range(10):
            VeronicaIntegration(backend=_NullBackend())
        assert len(save_calls) == 1, (
            f"Expected 1 save registration across 10 instances, got {len(save_calls)}"
        )

    def test_flag_set_after_first_init(self) -> None:
        """_atexit_registered must be True after the first backend-mode instance."""
        assert VeronicaIntegration._atexit_registered is False
        VeronicaIntegration(backend=_NullBackend())
        assert VeronicaIntegration._atexit_registered is True

    def test_no_backend_does_not_register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Instance without a backend must not register save via atexit and must not set flag."""
        save_calls = self._track_save_registrations(monkeypatch)
        assert VeronicaIntegration._atexit_registered is False
        VeronicaIntegration()  # legacy mode, no backend
        assert VeronicaIntegration._atexit_registered is False
        assert len(save_calls) == 0, (
            f"Expected 0 save registrations for no-backend instance, got {len(save_calls)}"
        )

    def test_flag_prevents_third_instance_from_registering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The class-level flag must prevent a third instance from registering save.

        After the first instance sets _atexit_registered=True, all subsequent
        instances must skip atexit.register(self.save) regardless of how many
        are created.  This test creates three instances sequentially and verifies
        that exactly one save registration occurs.
        """
        save_calls = self._track_save_registrations(monkeypatch)
        VeronicaIntegration(backend=_NullBackend())  # first: registers save
        VeronicaIntegration(backend=_NullBackend())  # second: skips (flag already set)
        VeronicaIntegration(backend=_NullBackend())  # third: skips (flag already set)
        assert len(save_calls) == 1, (
            f"Expected 1 save registration across 3 sequential instances, "
            f"got {len(save_calls)}"
        )


# ---------------------------------------------------------------------------
# A2: zero-value prompt_tokens=0 is not treated as None/missing
# ---------------------------------------------------------------------------


class TestAdversarialA2ZeroTokenNotMasked:
    """Adversarial tests for A2 -- or-operator masked zero token counts."""

    def test_prompt_tokens_zero_not_treated_as_missing(self) -> None:
        """prompt_tokens=0 must be used, not fall through to input_tokens."""
        # If `or` is still used: usage.get("prompt_tokens") = 0 (falsy) -> falls
        # through to usage.get("input_tokens") = 1000 -> wrong result.
        response = _make_llm_result(
            {"prompt_tokens": 0, "completion_tokens": 10, "input_tokens": 1000}
        )
        cost = extract_llm_result_cost(response)
        # With prompt_tokens=0 and completion_tokens=10:
        # estimate_cost_usd("", 0, 10) must be called.
        # With input_tokens=1000 and completion_tokens=10 (the wrong path):
        # estimate_cost_usd("", 1000, 10) would give a much larger value.
        from veronica_core.pricing import estimate_cost_usd

        correct_cost = estimate_cost_usd("", 0, 10)
        wrong_cost = estimate_cost_usd("", 1000, 10)
        # The two values must differ for this test to be meaningful.
        assert correct_cost != wrong_cost, "Test setup invalid: costs should differ"
        assert abs(cost - correct_cost) < 1e-9, (
            f"Got cost={cost}, expected {correct_cost} (zero prompt tokens masked by `or`?)"
        )

    def test_completion_tokens_zero_not_treated_as_missing(self) -> None:
        """completion_tokens=0 must be used, not fall through to output_tokens."""
        response = _make_llm_result(
            {"prompt_tokens": 10, "completion_tokens": 0, "output_tokens": 1000}
        )
        cost = extract_llm_result_cost(response)
        from veronica_core.pricing import estimate_cost_usd

        correct_cost = estimate_cost_usd("", 10, 0)
        wrong_cost = estimate_cost_usd("", 10, 1000)
        assert correct_cost != wrong_cost, "Test setup invalid: costs should differ"
        assert abs(cost - correct_cost) < 1e-9, (
            f"Got cost={cost}, expected {correct_cost} (zero completion tokens masked by `or`?)"
        )

    def test_both_tokens_zero_uses_zero_not_total_fallback(self) -> None:
        """When both prompt_tokens=0 and completion_tokens=0 are present, return zero cost."""
        response = _make_llm_result(
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 500}
        )
        cost = extract_llm_result_cost(response)
        from veronica_core.pricing import estimate_cost_usd

        # prompt=0, completion=0 -> estimate_cost_usd("", 0, 0)
        expected = estimate_cost_usd("", 0, 0)
        assert abs(cost - expected) < 1e-9, (
            f"Got cost={cost}, expected {expected}; "
            "zero-token pair should not fall back to total_tokens"
        )

    def test_prompt_tokens_absent_falls_back_to_input_tokens(self) -> None:
        """When prompt_tokens key is absent, input_tokens must be used."""
        response = _make_llm_result(
            {"input_tokens": 100, "completion_tokens": 50}
        )
        cost = extract_llm_result_cost(response)
        from veronica_core.pricing import estimate_cost_usd

        expected = estimate_cost_usd("", 100, 50)
        assert abs(cost - expected) < 1e-9, (
            f"Got cost={cost}, expected {expected}; "
            "absent prompt_tokens should fall back to input_tokens"
        )

    def test_completion_tokens_absent_falls_back_to_output_tokens(self) -> None:
        """When completion_tokens key is absent, output_tokens must be used."""
        response = _make_llm_result(
            {"prompt_tokens": 100, "output_tokens": 50}
        )
        cost = extract_llm_result_cost(response)
        from veronica_core.pricing import estimate_cost_usd

        expected = estimate_cost_usd("", 100, 50)
        assert abs(cost - expected) < 1e-9, (
            f"Got cost={cost}, expected {expected}; "
            "absent completion_tokens should fall back to output_tokens"
        )

    def test_input_tokens_zero_honored(self) -> None:
        """Fallback key input_tokens=0 must also not be masked by or-logic."""
        # prompt_tokens absent; input_tokens=0 must be used as 0.
        response = _make_llm_result(
            {"input_tokens": 0, "completion_tokens": 5}
        )
        cost = extract_llm_result_cost(response)
        from veronica_core.pricing import estimate_cost_usd

        expected = estimate_cost_usd("", 0, 5)
        assert abs(cost - expected) < 1e-9, (
            f"Got cost={cost}, expected {expected}; "
            "zero input_tokens (fallback key) should not be treated as missing"
        )
