"""veronica_core.patch — Optional SDK client patching for automatic injection.

Provides opt-in monkey-patching of OpenAI and Anthropic SDK calls so that
they are automatically checked against the active veronica_guard policy
boundary without modifying call sites.

NOT applied on import. Must be explicitly activated via patch_openai() /
patch_anthropic(). Safe to call when the SDK is not installed.

Public API:
    patch_openai()    — patch OpenAI chat.completions and legacy ChatCompletion
    patch_anthropic() — patch Anthropic Messages.create
    unpatch_all()     — restore all original methods
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from veronica_core.inject import get_active_container, is_guard_active

logger = logging.getLogger(__name__)

__all__ = ["patch_openai", "patch_anthropic", "unpatch_all"]

# Registry: key -> (target_class, attr_name, original_callable)
_patches: Dict[str, tuple] = {}


def _estimate_cost_openai(response: Any) -> float:
    """Rough token-cost estimate from an OpenAI response. Returns 0.0 if unavailable."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        total = getattr(usage, "total_tokens", 0)
        return total * 0.000002  # conservative $0.002 / 1K tokens
    except Exception:
        return 0.0


def _estimate_cost_anthropic(response: Any) -> float:
    """Rough token-cost estimate from an Anthropic response. Returns 0.0 if unavailable."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        inp = getattr(usage, "input_tokens", 0)
        out = getattr(usage, "output_tokens", 0)
        return (inp + out) * 0.000003  # conservative $0.003 / 1K tokens
    except Exception:
        return 0.0


def _make_patched(original: Callable, cost_fn: Callable, label: str) -> Callable:
    """Return a guard-aware wrapper around *original*."""
    import functools

    @functools.wraps(original)
    def patched(*args: Any, **kwargs: Any) -> Any:
        if not is_guard_active():
            return original(*args, **kwargs)

        container = get_active_container()

        # Pre-call: check policies (projected cost unknown -> 0.0)
        if container is not None:
            decision = container.check(cost_usd=0.0)
            if not decision.allowed:
                from veronica_core.inject import VeronicaHalt
                raise VeronicaHalt(decision.reason, decision)

        response = original(*args, **kwargs)

        # Post-call: record actual token cost
        if container is not None and container.budget is not None:
            cost = cost_fn(response)
            within = container.budget.spend(cost)
            if not within:
                logger.warning(
                    "[VERONICA_PATCH] %s pushed budget over limit "
                    "(spent $%.4f / $%.4f)",
                    label,
                    container.budget.spent_usd,
                    container.budget.limit_usd,
                )

        return response

    return patched


def patch_openai() -> None:
    """Patch the OpenAI SDK to enforce veronica_guard boundaries.

    Patches:
    - openai.resources.chat.completions.Completions.create (v1.x+)
    - openai.ChatCompletion.create (v0.x legacy, if present)

    Safe to call when openai is not installed (logs a warning, returns).
    Idempotent: subsequent calls after the first are no-ops.
    """
    patched_any = False

    # Modern OpenAI (v1.x)
    if "openai_modern" not in _patches:
        try:
            import openai.resources.chat.completions as _mod  # type: ignore[import]
            orig = _mod.Completions.create
            _mod.Completions.create = _make_patched(orig, _estimate_cost_openai, "openai")
            _patches["openai_modern"] = (_mod.Completions, "create", orig)
            logger.info("[VERONICA_PATCH] Patched openai.resources.chat.completions.Completions.create")
            patched_any = True
        except (ImportError, AttributeError):
            pass

    # Legacy OpenAI (v0.x)
    if "openai_legacy" not in _patches:
        try:
            import openai as _oa  # type: ignore[import]
            cls = getattr(_oa, "ChatCompletion", None)
            if cls is not None:
                orig = cls.create
                cls.create = _make_patched(orig, _estimate_cost_openai, "openai-legacy")
                _patches["openai_legacy"] = (cls, "create", orig)
                logger.info("[VERONICA_PATCH] Patched openai.ChatCompletion.create")
                patched_any = True
        except (ImportError, AttributeError, TypeError):
            pass

    if not patched_any and "openai_modern" not in _patches and "openai_legacy" not in _patches:
        logger.warning("[VERONICA_PATCH] openai not installed or no patchable targets found")


def patch_anthropic() -> None:
    """Patch the Anthropic SDK to enforce veronica_guard boundaries.

    Patches anthropic.resources.messages.Messages.create.

    Safe to call when anthropic is not installed (logs a warning, returns).
    Idempotent: subsequent calls after the first are no-ops.
    """
    if "anthropic" in _patches:
        logger.debug("[VERONICA_PATCH] Anthropic already patched, skipping")
        return

    try:
        import anthropic.resources.messages as _mod  # type: ignore[import]
        orig = _mod.Messages.create
        _mod.Messages.create = _make_patched(orig, _estimate_cost_anthropic, "anthropic")
        _patches["anthropic"] = (_mod.Messages, "create", orig)
        logger.info("[VERONICA_PATCH] Patched anthropic.resources.messages.Messages.create")
    except (ImportError, AttributeError):
        logger.warning("[VERONICA_PATCH] anthropic not installed or no patchable targets found")


def unpatch_all() -> None:
    """Restore all patched SDK methods to their originals.

    Safe to call when nothing is patched (no-op).
    """
    for key, (cls, attr, original) in list(_patches.items()):
        setattr(cls, attr, original)
        logger.info("[VERONICA_PATCH] Restored %s.%s", key, attr)
    _patches.clear()
