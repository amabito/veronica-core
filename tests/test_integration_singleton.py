"""Tests for get_veronica_integration() singleton factory."""

from __future__ import annotations

import pytest

from veronica_core.backends import MemoryBackend


class TestGetVeronicaIntegrationSingleton:
    """Tests for get_veronica_integration() -- singleton creation and behavior."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Reset the global singleton between tests to ensure isolation."""
        import veronica_core.integration as _mod

        original = _mod._veronica_integration
        _mod._veronica_integration = None
        yield
        _mod._veronica_integration = None
        # Restore original so we don't leave None in a shared environment.
        if original is not None:
            _mod._veronica_integration = original

    def test_basic_call_returns_integration_instance(self) -> None:
        """get_veronica_integration() returns a VeronicaIntegration."""
        from veronica_core import VeronicaIntegration
        from veronica_core.integration import get_veronica_integration

        # Patch in a memory backend so no JSON file is created.
        import veronica_core.integration as _mod
        from veronica_core.backends import MemoryBackend

        # Override backend to avoid disk access.
        original_init = _mod.VeronicaIntegration.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("backend", MemoryBackend())
            original_init(self, **kwargs)

        _mod.VeronicaIntegration.__init__ = patched_init
        try:
            instance = get_veronica_integration()
            assert isinstance(instance, VeronicaIntegration)
        finally:
            _mod.VeronicaIntegration.__init__ = original_init

    def test_singleton_returns_same_instance_on_second_call(self) -> None:
        """Calling get_veronica_integration() twice returns the same object."""
        import veronica_core.integration as _mod
        from veronica_core.integration import get_veronica_integration

        original_init = _mod.VeronicaIntegration.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("backend", MemoryBackend())
            original_init(self, **kwargs)

        _mod.VeronicaIntegration.__init__ = patched_init
        try:
            first = get_veronica_integration()
            second = get_veronica_integration()
            assert first is second
        finally:
            _mod.VeronicaIntegration.__init__ = original_init

    def test_singleton_second_call_with_different_params_returns_same_instance(
        self,
    ) -> None:
        """A second call with different params still returns the existing singleton."""
        import veronica_core.integration as _mod
        from veronica_core.integration import get_veronica_integration

        original_init = _mod.VeronicaIntegration.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("backend", MemoryBackend())
            original_init(self, **kwargs)

        _mod.VeronicaIntegration.__init__ = patched_init
        try:
            first = get_veronica_integration(cooldown_fails=3)
            second = get_veronica_integration(cooldown_fails=99)
            assert first is second
            # Original config must be preserved
            assert first.state.cooldown_fails == 3
        finally:
            _mod.VeronicaIntegration.__init__ = original_init
