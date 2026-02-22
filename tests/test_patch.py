"""Tests for veronica_core.patch -- SDK monkey-patching with guard-context awareness."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock
import pytest

from veronica_core import veronica_guard
from veronica_core.inject import get_active_container, is_guard_active
from veronica_core.patch import patch_anthropic, patch_openai, unpatch_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_response() -> MagicMock:
    resp = MagicMock()
    resp.usage.total_tokens = 100
    return resp


def _anthropic_response() -> MagicMock:
    resp = MagicMock()
    resp.usage.input_tokens = 50
    resp.usage.output_tokens = 50
    return resp


def _inject_fake_openai():
    """Inject minimal openai v1 module tree into sys.modules."""
    oa = types.ModuleType("openai")
    resources = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    comp_mod = types.ModuleType("openai.resources.chat.completions")

    class FakeCompletions:
        def create(self, *args, **kwargs):
            return _openai_response()

    comp_mod.Completions = FakeCompletions
    chat_mod.completions = comp_mod
    resources.chat = chat_mod
    oa.resources = resources

    sys.modules.update({
        "openai": oa,
        "openai.resources": resources,
        "openai.resources.chat": chat_mod,
        "openai.resources.chat.completions": comp_mod,
    })
    return comp_mod


def _inject_fake_anthropic():
    """Inject minimal anthropic module tree into sys.modules."""
    ant = types.ModuleType("anthropic")
    res_mod = types.ModuleType("anthropic.resources")
    msg_mod = types.ModuleType("anthropic.resources.messages")

    class FakeMessages:
        def create(self, *args, **kwargs):
            return _anthropic_response()

    msg_mod.Messages = FakeMessages
    res_mod.messages = msg_mod
    ant.resources = res_mod

    sys.modules.update({
        "anthropic": ant,
        "anthropic.resources": res_mod,
        "anthropic.resources.messages": msg_mod,
    })
    return msg_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_patches():
    """Ensure patches are cleared before and after every test."""
    unpatch_all()
    yield
    unpatch_all()


# ---------------------------------------------------------------------------
# patch_openai
# ---------------------------------------------------------------------------


class TestPatchOpenAI:
    def test_outside_guard_passthrough(self):
        """Outside guard: original is called unchanged."""
        comp_mod = _inject_fake_openai()
        calls = []

        def tracking(self, *a, **kw):
            calls.append("ok")
            return _openai_response()

        comp_mod.Completions.create = tracking
        patch_openai()

        assert not is_guard_active()
        comp_mod.Completions().create(model="gpt-4", messages=[])
        assert calls == ["ok"]

    def test_inside_guard_original_called(self):
        """Inside guard: original is still invoked."""
        comp_mod = _inject_fake_openai()
        calls = []

        def tracking(self, *a, **kw):
            calls.append("ok")
            return _openai_response()

        comp_mod.Completions.create = tracking
        patch_openai()

        @veronica_guard(max_cost_usd=10.0)
        def agent():
            comp_mod.Completions().create(model="gpt-4", messages=[])

        agent()
        assert calls == ["ok"]

    def test_inside_guard_budget_spend_recorded(self):
        """Inside guard: budget.spend() is called after response."""
        comp_mod = _inject_fake_openai()
        comp_mod.Completions.create = lambda self, *a, **kw: _openai_response()
        patch_openai()

        captured: list = []

        @veronica_guard(max_cost_usd=10.0)
        def agent():
            comp_mod.Completions().create(model="gpt-4", messages=[])
            # Capture container from inside the call (per-call design)
            captured.append(get_active_container())

        agent()
        assert captured, "Container should have been captured from inside the call"
        assert captured[0].budget.call_count >= 1

    def test_pre_exhausted_budget_raises_veronica_halt(self):
        """Pre-exhausted budget inside guard raises VeronicaHalt via patch."""
        from veronica_core.inject import VeronicaHalt

        comp_mod = _inject_fake_openai()
        comp_mod.Completions.create = lambda self, *a, **kw: _openai_response()
        patch_openai()

        @veronica_guard(max_cost_usd=1.0)
        def agent():
            # Exhaust the budget before making the SDK call
            container = get_active_container()
            container.budget.spend(2.0)  # _spent_usd=2.0 > limit=1.0
            comp_mod.Completions().create(model="gpt-4", messages=[])

        with pytest.raises(VeronicaHalt):
            agent()

    def test_unpatch_restores_original(self):
        """unpatch_all restores the original method."""
        comp_mod = _inject_fake_openai()
        original = comp_mod.Completions.create
        patch_openai()
        assert comp_mod.Completions.create is not original
        unpatch_all()
        assert comp_mod.Completions.create is original

    def test_double_patch_is_idempotent(self):
        """Calling patch_openai twice does not double-wrap."""
        comp_mod = _inject_fake_openai()
        patch_openai()
        after_first = comp_mod.Completions.create
        patch_openai()
        assert comp_mod.Completions.create is after_first


# ---------------------------------------------------------------------------
# patch_anthropic
# ---------------------------------------------------------------------------


class TestPatchAnthropic:
    def test_outside_guard_passthrough(self):
        """Outside guard: Anthropic original is called unchanged."""
        msg_mod = _inject_fake_anthropic()
        calls = []

        def tracking(self, *a, **kw):
            calls.append("ok")
            return _anthropic_response()

        msg_mod.Messages.create = tracking
        patch_anthropic()
        msg_mod.Messages().create(model="claude-3-5-sonnet-20241022", messages=[])
        assert calls == ["ok"]

    def test_inside_guard_original_called(self):
        """Inside guard: Anthropic original is still invoked."""
        msg_mod = _inject_fake_anthropic()
        calls = []

        def tracking(self, *a, **kw):
            calls.append("ok")
            return _anthropic_response()

        msg_mod.Messages.create = tracking
        patch_anthropic()

        @veronica_guard(max_cost_usd=10.0)
        def agent():
            msg_mod.Messages().create(model="claude-3-5-sonnet-20241022", messages=[])

        agent()
        assert calls == ["ok"]

    def test_unpatch_restores_original(self):
        """unpatch_all restores the original Anthropic method."""
        msg_mod = _inject_fake_anthropic()
        original = msg_mod.Messages.create
        patch_anthropic()
        assert msg_mod.Messages.create is not original
        unpatch_all()
        assert msg_mod.Messages.create is original


# ---------------------------------------------------------------------------
# unpatch_all
# ---------------------------------------------------------------------------


class TestUnpatchAll:
    def test_noop_when_nothing_patched(self):
        unpatch_all()  # must not raise

    def test_can_repatch_after_unpatch(self):
        """After unpatch_all, patch_openai can be called again cleanly."""
        comp_mod = _inject_fake_openai()
        patch_openai()
        unpatch_all()
        original = comp_mod.Completions.create
        patch_openai()
        assert comp_mod.Completions.create is not original


# ---------------------------------------------------------------------------
# SDK not installed
# ---------------------------------------------------------------------------


class TestSdkNotInstalled:
    def test_patch_openai_safe_when_not_installed(self):
        saved = {k: v for k, v in sys.modules.items() if k.startswith("openai")}
        for k in saved:
            sys.modules.pop(k)
        try:
            patch_openai()  # must not raise
        finally:
            sys.modules.update(saved)

    def test_patch_anthropic_safe_when_not_installed(self):
        saved = {k: v for k, v in sys.modules.items() if k.startswith("anthropic")}
        for k in saved:
            sys.modules.pop(k)
        try:
            patch_anthropic()  # must not raise
        finally:
            sys.modules.update(saved)
