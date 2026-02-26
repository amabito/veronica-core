"""Tests for VeronicaExit backward compatibility shims.

Covers:
  - New PersistenceBackend (new API) accepted without warning.
  - Legacy VeronicaPersistence (old API: save(state_machine)) accepted with
    DeprecationWarning and still saves without error.
  - ShieldPipeline.on_error_policy: default=HALT, explicit ALLOW opt-in.
"""
from __future__ import annotations

import warnings
from typing import Optional
from unittest.mock import MagicMock

import pytest

from veronica_core.backends import MemoryBackend, PersistenceBackend
from veronica_core.exit import VeronicaExit
from veronica_core.shield.pipeline import ShieldPipeline
from veronica_core.shield.types import Decision, ToolCallContext
from veronica_core.state import VeronicaStateMachine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_machine() -> VeronicaStateMachine:
    return VeronicaStateMachine()


CTX = ToolCallContext(request_id="compat-test")


# ---------------------------------------------------------------------------
# Test: VeronicaExit with new PersistenceBackend (no warning)
# ---------------------------------------------------------------------------

class TestVeronicaExitNewBackend:
    def test_new_backend_accepted_without_warning(self) -> None:
        sm = _make_state_machine()
        backend = MemoryBackend()

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # Must not raise DeprecationWarning.
            ve = VeronicaExit(state_machine=sm, persistence=backend)

        assert ve.persistence is backend

    def test_new_backend_save_called_with_dict(self) -> None:
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        # _graceful_exit calls persistence.save(state_machine.to_dict())
        ve._graceful_exit()

        loaded = backend.load()
        assert loaded is not None
        assert isinstance(loaded, dict)


# ---------------------------------------------------------------------------
# Test: VeronicaExit with legacy VeronicaPersistence (warns + works)
# ---------------------------------------------------------------------------

class TestVeronicaExitLegacyBackend:
    """Legacy VeronicaPersistence expects save(state_machine), not save(dict).

    The adapter wraps the old object so VeronicaExit can call save(dict)
    transparently.
    """

    def _make_legacy_persistence(self) -> object:
        """Minimal VeronicaPersistence stand-in (old API)."""
        from veronica_core.state import VeronicaStateMachine as _SM

        class LegacyPersistence:
            def __init__(self) -> None:
                self.saved: Optional[dict] = None

            def save(self, state_machine: _SM) -> bool:  # old signature
                self.saved = state_machine.to_dict()
                return True

            def load(self) -> Optional[_SM]:
                return None

        return LegacyPersistence()

    def test_legacy_backend_emits_deprecation_warning(self) -> None:
        sm = _make_state_machine()
        legacy = self._make_legacy_persistence()

        with pytest.warns(DeprecationWarning, match="VeronicaPersistence"):
            VeronicaExit(state_machine=sm, persistence=legacy)

    def test_legacy_backend_save_succeeds(self) -> None:
        sm = _make_state_machine()
        legacy = self._make_legacy_persistence()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ve = VeronicaExit(state_machine=sm, persistence=legacy)

        # Should not raise TypeError even though legacy.save() expects a state machine.
        ve._graceful_exit()

        assert legacy.saved is not None
        assert isinstance(legacy.saved, dict)


# ---------------------------------------------------------------------------
# Test: ShieldPipeline.on_error default HALT and explicit ALLOW
# ---------------------------------------------------------------------------

class TestShieldPipelineOnErrorPolicy:
    def test_default_no_hook_halts(self) -> None:
        """No retry hook + default policy → HALT (fail-closed)."""
        pipe = ShieldPipeline()
        result = pipe.on_error(CTX, RuntimeError("boom"))
        assert result is Decision.HALT

    def test_explicit_allow_policy_no_hook(self) -> None:
        """Caller opts in to old ALLOW behaviour explicitly."""
        pipe = ShieldPipeline(on_error_policy=Decision.ALLOW)
        result = pipe.on_error(CTX, RuntimeError("boom"))
        assert result is Decision.ALLOW

    def test_explicit_halt_policy_no_hook(self) -> None:
        """Explicit HALT matches default behaviour."""
        pipe = ShieldPipeline(on_error_policy=Decision.HALT)
        result = pipe.on_error(CTX, RuntimeError("boom"))
        assert result is Decision.HALT

    def test_hook_present_overrides_policy(self) -> None:
        """When hook is registered its decision takes precedence over policy."""
        from veronica_core.shield.hooks import RetryBoundaryHook

        class AllowRetryHook(RetryBoundaryHook):
            def on_error(self, ctx: ToolCallContext, err: BaseException) -> Optional[Decision]:
                return Decision.ALLOW

        # Policy is HALT but hook returns ALLOW → hook wins.
        pipe = ShieldPipeline(
            retry=AllowRetryHook(),
            on_error_policy=Decision.HALT,
        )
        result = pipe.on_error(CTX, RuntimeError("err"))
        assert result is Decision.ALLOW
