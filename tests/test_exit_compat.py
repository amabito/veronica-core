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

import pytest

from veronica_core.backends import MemoryBackend
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
            def on_error(
                self, ctx: ToolCallContext, err: BaseException
            ) -> Optional[Decision]:
                return Decision.ALLOW

        # Policy is HALT but hook returns ALLOW → hook wins.
        pipe = ShieldPipeline(
            retry=AllowRetryHook(),
            on_error_policy=Decision.HALT,
        )
        result = pipe.on_error(CTX, RuntimeError("err"))
        assert result is Decision.ALLOW


# ---------------------------------------------------------------------------
# Test: VeronicaPersistence __getattr__ DeprecationWarning (Phase 0, item 0c)
# ---------------------------------------------------------------------------


class TestVeronicaPersistenceGetattr:
    """Accessing veronica_core.VeronicaPersistence must emit DeprecationWarning."""

    def test_module_getattr_emits_deprecation_warning(self) -> None:
        import veronica_core

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            _ = veronica_core.VeronicaPersistence
        assert any(
            issubclass(warning.category, DeprecationWarning)
            and "VeronicaPersistence" in str(warning.message)
            for warning in w
        ), "Accessing VeronicaPersistence on veronica_core must emit DeprecationWarning"

    def test_veronica_persistence_not_in_all(self) -> None:
        import veronica_core

        assert "VeronicaPersistence" not in veronica_core.__all__

    def test_veronica_persistence_returns_correct_class(self) -> None:
        import veronica_core

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            klass = veronica_core.VeronicaPersistence
        from veronica_core.persist import VeronicaPersistence as _VP

        assert klass is _VP

    def test_unknown_module_getattr_raises_attribute_error(self) -> None:
        """Accessing a non-existent name on veronica_core must raise AttributeError."""
        import veronica_core

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = veronica_core.TotallyBogusName123


# ---------------------------------------------------------------------------
# Test: VeronicaExit exception safety during shutdown (v1.8.10)
# ---------------------------------------------------------------------------


class TestVeronicaExitExceptionSafety:
    """Exit handlers must not propagate exceptions from state machine or persistence."""

    def test_graceful_exit_survives_transition_failure(self) -> None:
        """_graceful_exit must complete even if state_machine.transition() raises."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        # Sabotage transition to raise
        _original_transition = sm.transition

        def failing_transition(*args, **kwargs):
            raise RuntimeError("transition broken")

        sm.transition = failing_transition

        # Must not raise
        ve._graceful_exit()

        # State save should still have been attempted
        loaded = backend.load()
        assert loaded is not None

    def test_graceful_exit_survives_persistence_failure(self) -> None:
        """_graceful_exit must complete even if persistence.save() raises."""
        sm = _make_state_machine()

        class FailingSaveBackend(MemoryBackend):
            def save(self, data: dict) -> bool:
                raise IOError("disk full")

        backend = FailingSaveBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        # Must not raise
        ve._graceful_exit()

    def test_emergency_exit_survives_transition_failure(self) -> None:
        """_emergency_exit must complete even if state_machine.transition() raises."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        sm.transition = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("broken"))

        # Must not raise
        ve._emergency_exit()

    def test_emergency_exit_survives_persistence_failure(self) -> None:
        """_emergency_exit must complete even if persistence.save() raises."""
        sm = _make_state_machine()

        class FailingSaveBackend(MemoryBackend):
            def save(self, data: dict) -> bool:
                raise IOError("disk full")

        backend = FailingSaveBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        # Must not raise
        ve._emergency_exit()


# ---------------------------------------------------------------------------
# Test: VeronicaExit coverage gaps - signal handler, request_exit, force exit
# ---------------------------------------------------------------------------


class TestVeronicaExitCoverage:
    """Cover previously uncovered exit paths: signal handler, duplicate request, force."""

    def test_request_exit_duplicate_is_ignored(self) -> None:
        """Second request_exit call must be ignored (idempotent)."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        ve.request_exit(ExitTier.GRACEFUL, "first")
        # Second call must not raise and must not change the tier
        ve.request_exit(ExitTier.EMERGENCY, "second")

        assert ve.exit_tier == ExitTier.GRACEFUL
        assert ve.exit_reason == "first"

    def test_request_exit_force_tier(self) -> None:
        """FORCE exit tier executes without saving state."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        ve.request_exit(ExitTier.FORCE, "forced")

        # Force exit should not persist state
        assert ve.is_exit_requested()
        assert ve.exit_tier == ExitTier.FORCE

    def test_is_exit_requested_false_initially(self) -> None:
        """is_exit_requested() must return False before any request."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)
        assert not ve.is_exit_requested()

    def test_signal_handler_sigterm_triggers_graceful(self) -> None:
        """_signal_handler with SIGTERM must request GRACEFUL exit."""
        import signal as _signal

        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        ve._signal_handler(_signal.SIGTERM, None)
        assert ve.exit_requested
        assert ve.exit_tier == ExitTier.GRACEFUL

    def test_signal_handler_sigint_triggers_emergency(self) -> None:
        """_signal_handler with SIGINT must request EMERGENCY exit."""
        import signal as _signal

        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        ve._signal_handler(_signal.SIGINT, None)
        assert ve.exit_requested
        assert ve.exit_tier == ExitTier.EMERGENCY

    def test_atexit_handler_when_exit_not_requested(self) -> None:
        """_atexit_handler must trigger EMERGENCY exit if not already requested."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        assert not ve.exit_requested
        ve._atexit_handler()
        assert ve.exit_requested
        assert ve.exit_tier == ExitTier.EMERGENCY

    def test_atexit_handler_when_exit_already_requested_is_noop(self) -> None:
        """_atexit_handler must be a no-op when exit was already requested."""
        sm = _make_state_machine()
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        from veronica_core.exit import ExitTier

        ve.request_exit(ExitTier.GRACEFUL, "explicit")
        ve._atexit_handler()  # Must not change state

        assert ve.exit_tier == ExitTier.GRACEFUL
        assert ve.exit_reason == "explicit"

    def test_graceful_exit_save_returns_false(self) -> None:
        """_graceful_exit must complete even when persistence.save() returns False."""
        sm = _make_state_machine()

        class FalseReturnBackend(MemoryBackend):
            def save(self, data: dict) -> bool:
                return False

        ve = VeronicaExit(state_machine=sm, persistence=FalseReturnBackend())
        ve._graceful_exit()  # Must not raise

    def test_emergency_exit_save_returns_false(self) -> None:
        """_emergency_exit must complete even when persistence.save() returns False."""
        sm = _make_state_machine()

        class FalseReturnBackend(MemoryBackend):
            def save(self, data: dict) -> bool:
                return False

        ve = VeronicaExit(state_machine=sm, persistence=FalseReturnBackend())
        ve._emergency_exit()  # Must not raise

    def test_legacy_adapter_load_with_state_machine_result(self) -> None:
        """Legacy adapter load() must convert VeronicaStateMachine to dict."""
        from veronica_core.state import VeronicaStateMachine as _SM

        class LegacyWithLoad:
            def save(self, state_machine: _SM) -> bool:
                return True

            def load(self) -> _SM:
                return _SM()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from veronica_core.exit import _wrap_legacy_persistence

            adapted = _wrap_legacy_persistence(LegacyWithLoad())

        result = adapted.load()
        assert isinstance(result, dict)

    def test_legacy_adapter_load_with_none_result(self) -> None:
        """Legacy adapter load() returning None must pass through None."""
        from veronica_core.state import VeronicaStateMachine as _SM

        class LegacyWithNoneLoad:
            def save(self, state_machine: _SM) -> bool:
                return True

            def load(self):
                return None

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from veronica_core.exit import _wrap_legacy_persistence

            adapted = _wrap_legacy_persistence(LegacyWithNoneLoad())

        assert adapted.load() is None


# ---------------------------------------------------------------------------
# Test: VeronicaPersistence (deprecated) coverage
# ---------------------------------------------------------------------------


class TestVeronicaPersistenceCoverage:
    """Cover persist.py paths: load success/fail, backup, str path coercion."""

    def _make_persist(self, tmp_path) -> object:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from veronica_core.persist import VeronicaPersistence

            return VeronicaPersistence(path=tmp_path / "state.json")

    def test_str_path_coercion(self, tmp_path) -> None:
        """VeronicaPersistence must accept str path (coerced to Path)."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from veronica_core.persist import VeronicaPersistence

            p = VeronicaPersistence(path=str(tmp_path / "state.json"))
        from pathlib import Path

        assert isinstance(p.path, Path)

    def test_save_and_load_roundtrip(self, tmp_path) -> None:
        """save() then load() must recover the same state."""
        from veronica_core.state import VeronicaStateMachine

        p = self._make_persist(tmp_path)
        sm = VeronicaStateMachine()
        assert p.save(sm) is True

        loaded = p.load()
        assert loaded is not None
        assert isinstance(loaded, VeronicaStateMachine)

    def test_load_missing_file_returns_none(self, tmp_path) -> None:
        """load() when file doesn't exist must return None."""
        p = self._make_persist(tmp_path)
        # No save — file doesn't exist
        result = p.load()
        assert result is None

    def test_load_invalid_json_returns_none(self, tmp_path) -> None:
        """load() with corrupt JSON must return None without raising."""
        p = self._make_persist(tmp_path)
        # Write garbage JSON
        p.path.write_text("NOT_VALID_JSON{{{")
        result = p.load()
        assert result is None

    def test_save_failure_returns_false(self, tmp_path) -> None:
        """save() when writing fails must return False."""
        from veronica_core.state import VeronicaStateMachine

        p = self._make_persist(tmp_path)
        # Make path a directory so open() fails
        p.path.mkdir(parents=True, exist_ok=True)
        result = p.save(VeronicaStateMachine())
        assert result is False

    def test_backup_creates_file(self, tmp_path) -> None:
        """backup() must create a timestamped copy of the state file."""
        from veronica_core.state import VeronicaStateMachine

        p = self._make_persist(tmp_path)
        p.save(VeronicaStateMachine())

        result = p.backup()
        assert result is True
        backup_files = list(tmp_path.glob("*_backup_*.json"))
        assert len(backup_files) == 1

    def test_backup_when_file_missing_returns_false(self, tmp_path) -> None:
        """backup() when state file doesn't exist must return False."""
        p = self._make_persist(tmp_path)
        result = p.backup()
        assert result is False

    def test_backup_failure_returns_false(self, tmp_path) -> None:
        """backup() must return False when shutil.copy2 raises."""
        from unittest.mock import patch
        from veronica_core.state import VeronicaStateMachine

        p = self._make_persist(tmp_path)
        p.save(VeronicaStateMachine())

        with patch("shutil.copy2", side_effect=OSError("permission denied")):
            result = p.backup()

        assert result is False


class TestVeronicaExitGracefulEdgePaths:
    """Cover the remaining uncovered branches in _graceful_exit."""

    def test_graceful_exit_with_expired_cooldowns_logged(self) -> None:
        """_graceful_exit must log when cleanup_expired() returns non-empty list."""
        import time
        from veronica_core.state import VeronicaStateMachine

        sm = VeronicaStateMachine()
        # Add a cooldown that expires immediately
        sm.cooldowns["test-agent"] = time.time() - 1.0  # already expired
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        # Must not raise; L149 branch (expired list non-empty) is covered
        ve._graceful_exit()

    def test_graceful_exit_cleanup_raises_is_swallowed(self) -> None:
        """_graceful_exit must continue when cleanup_expired() raises."""
        sm = _make_state_machine()
        sm.cleanup_expired = lambda: (_ for _ in ()).throw(
            RuntimeError("cleanup error")
        )
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        ve._graceful_exit()  # Must not raise (L150-151 branch covered)

    def test_graceful_exit_get_stats_raises_is_swallowed(self) -> None:
        """_graceful_exit must complete when get_stats() raises."""
        sm = _make_state_machine()
        sm.get_stats = lambda: (_ for _ in ()).throw(RuntimeError("stats error"))
        backend = MemoryBackend()
        ve = VeronicaExit(state_machine=sm, persistence=backend)

        ve._graceful_exit()  # Must not raise (L167-168 branch covered)
