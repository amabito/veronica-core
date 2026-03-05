"""Tests for veronica_core.adapters._shared.check_and_halt helper.

Verifies the unified HALT detection pattern used by all framework adapters.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from veronica_core.adapters._shared import check_and_halt
from veronica_core.inject import VeronicaHalt
from veronica_core.runtime_policy import PolicyDecision


def _make_container(allowed: bool, reason: str = "") -> MagicMock:
    """Build a minimal mock container for check_and_halt tests."""
    container = MagicMock()
    container.check.return_value = PolicyDecision(
        allowed=allowed, reason=reason, policy_type="test"
    )
    return container


class TestCheckAndHaltBasic:
    """Basic happy-path and denial behaviour."""

    def test_allowed_does_not_raise(self) -> None:
        container = _make_container(allowed=True)
        check_and_halt(container)  # must not raise

    def test_denied_raises_veronica_halt(self) -> None:
        container = _make_container(allowed=False, reason="budget exceeded")
        with pytest.raises(VeronicaHalt) as exc_info:
            check_and_halt(container)
        assert exc_info.value.reason == "budget exceeded"

    def test_halt_carries_decision(self) -> None:
        container = _make_container(allowed=False, reason="step limit")
        with pytest.raises(VeronicaHalt) as exc_info:
            check_and_halt(container)
        assert exc_info.value.decision is not None
        assert not exc_info.value.decision.allowed

    def test_check_called_with_zero_cost(self) -> None:
        container = _make_container(allowed=True)
        check_and_halt(container)
        container.check.assert_called_once_with(cost_usd=0.0)


class TestCheckAndHaltTagAndLogger:
    """Tag and logger arguments."""

    def test_custom_tag_logged_on_denial(self, caplog: pytest.LogCaptureFixture) -> None:
        container = _make_container(allowed=False, reason="step limit")
        with caplog.at_level(logging.DEBUG, logger="veronica_core.adapters._shared"):
            with pytest.raises(VeronicaHalt):
                check_and_halt(container, tag="[VERONICA_LC]")
        assert "[VERONICA_LC]" in caplog.text

    def test_custom_logger_used_on_denial(self) -> None:
        custom_logger = MagicMock(spec=logging.Logger)
        container = _make_container(allowed=False, reason="budget")
        with pytest.raises(VeronicaHalt):
            check_and_halt(container, tag="[TEST]", _logger=custom_logger)
        custom_logger.debug.assert_called_once()
        call_args = custom_logger.debug.call_args[0]
        assert "[TEST]" in call_args[1]

    def test_no_log_on_allow(self, caplog: pytest.LogCaptureFixture) -> None:
        container = _make_container(allowed=True)
        with caplog.at_level(logging.DEBUG):
            check_and_halt(container, tag="[VERONICA_TEST]")
        assert "[VERONICA_TEST]" not in caplog.text


class TestCheckAndHaltAdversarial:
    """Adversarial: corrupted containers, edge-case reasons, type variants."""

    def test_empty_reason_on_denial(self) -> None:
        container = _make_container(allowed=False, reason="")
        with pytest.raises(VeronicaHalt) as exc_info:
            check_and_halt(container)
        assert exc_info.value.reason == ""

    def test_multiline_reason_preserved(self) -> None:
        reason = "budget\nexceeded\nstep limit"
        container = _make_container(allowed=False, reason=reason)
        with pytest.raises(VeronicaHalt) as exc_info:
            check_and_halt(container)
        assert exc_info.value.reason == reason

    def test_check_raises_propagates(self) -> None:
        """If container.check() itself raises, the exception propagates unchanged."""
        container = MagicMock()
        container.check.side_effect = RuntimeError("backend down")
        with pytest.raises(RuntimeError, match="backend down"):
            check_and_halt(container)

    def test_none_logger_falls_back_to_module_logger(self) -> None:
        """Passing _logger=None must not crash."""
        container = _make_container(allowed=False, reason="test")
        with pytest.raises(VeronicaHalt):
            check_and_halt(container, _logger=None)

    def test_default_tag_used_when_not_specified(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        container = _make_container(allowed=False, reason="x")
        with caplog.at_level(logging.DEBUG, logger="veronica_core.adapters._shared"):
            with pytest.raises(VeronicaHalt):
                check_and_halt(container)
        assert "[VERONICA]" in caplog.text
