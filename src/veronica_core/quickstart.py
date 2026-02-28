"""veronica_core.quickstart — 2-line setup API for AI cost containment.

Provides a minimal entry point that wraps the full ExecutionContext + patch
machinery behind a single ``init()`` call, competing with AgentBudget-style
one-liner setup.

Usage::

    import veronica_core

    ctx = veronica_core.init("$5.00")
    # ... your LLM calls are now cost-bounded ...
    veronica_core.shutdown()

    # Or as a context manager via the returned ExecutionContext:
    with veronica_core.init("$5.00"):
        ...

Thread-safety:
    ``init()`` and ``shutdown()`` are protected by a module-level Lock.
    ``get_context()`` is read-only and safe to call from any thread.
"""
from __future__ import annotations

import atexit
import logging
import math
import threading
from typing import Literal, Optional

from veronica_core.containment.execution_context import ExecutionConfig, ExecutionContext

__all__ = ["init", "shutdown", "get_context"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_context: Optional[ExecutionContext] = None
_initialized: bool = False

# Sentinel: atexit handler registered once.
_atexit_registered: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_budget(budget: str) -> float:
    """Parse a human-readable USD budget string to a positive float.

    Accepts:
        - "$5.00", "$10", "5.00", "10" (leading ``$`` optional)

    Args:
        budget: Budget string to parse.

    Returns:
        Positive float representing the USD ceiling.

    Raises:
        ValueError: When the string cannot be parsed or the value is not
            a positive finite number.
    """
    cleaned = budget.strip().lstrip("$").strip()
    if not cleaned:
        raise ValueError(f"Budget string is empty after stripping: {budget!r}")

    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(
            f"Cannot parse budget {budget!r}: not a valid number after stripping '$'"
        )

    if math.isnan(value):
        raise ValueError(f"Budget {budget!r} parsed to NaN, which is not allowed")
    if math.isinf(value):
        raise ValueError(f"Budget {budget!r} parsed to infinity, which is not allowed")
    if value <= 0.0:
        raise ValueError(
            f"Budget {budget!r} parsed to {value}, but budget must be positive (> 0)"
        )

    return value


def _atexit_handler() -> None:
    """Auto-cleanup registered via atexit on first init()."""
    shutdown()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init(
    budget: str,
    *,
    max_steps: int = 1000,
    max_retries_total: int = 50,
    timeout_ms: int = 0,
    on_halt: Literal["raise", "warn", "silent"] = "raise",
    patch_openai: bool = False,
    patch_anthropic: bool = False,
) -> ExecutionContext:
    """Initialize the VERONICA cost-containment layer.

    Creates a global ``ExecutionContext`` with the given budget and
    optionally monkey-patches OpenAI / Anthropic SDKs so that all LLM calls
    are automatically intercepted and counted.

    Args:
        budget: USD spending ceiling as a string, e.g. ``"$5.00"`` or ``"10"``.
            The leading ``$`` is optional. Must be a positive finite number.
        max_steps: Maximum number of successful LLM/tool calls before HALT.
            Defaults to 1000 (generous for most agentic workloads).
        max_retries_total: Chain-wide retry budget. Defaults to 50.
        timeout_ms: Wall-clock timeout in milliseconds. 0 disables the timeout.
        on_halt: Action taken when a HALT decision is returned by a wrap call.
            ``"raise"``  — raises :class:`~veronica_core.inject.VeronicaHalt`.
            ``"warn"``   — logs a warning and continues.
            ``"silent"`` — does nothing (caller must inspect the Decision).
        patch_openai: If True, monkey-patch the OpenAI SDK so that calls to
            ``chat.completions.create`` are automatically intercepted.
        patch_anthropic: If True, monkey-patch the Anthropic SDK so that
            calls to ``messages.create`` are automatically intercepted.

    Returns:
        The newly created :class:`~veronica_core.containment.ExecutionContext`.
        Power users may call ``wrap_llm_call`` / ``wrap_tool_call`` directly
        on the returned context.

    Raises:
        RuntimeError: When called a second time without an intervening
            ``shutdown()``.
        ValueError: When *budget* cannot be parsed as a positive number.

    Example::

        ctx = veronica_core.init("$5.00", patch_openai=True)
        # ... run agent ...
        veronica_core.shutdown()
    """
    global _context, _initialized, _atexit_registered

    max_cost_usd = _parse_budget(budget)

    with _lock:
        if _initialized:
            raise RuntimeError(
                "veronica_core.init() called while already initialized. "
                "Call veronica_core.shutdown() first."
            )

        config = ExecutionConfig(
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_retries_total=max_retries_total,
            timeout_ms=timeout_ms,
        )
        _context = ExecutionContext(config=config)
        _initialized = True

        if not _atexit_registered:
            atexit.register(_atexit_handler)
            _atexit_registered = True

    # Apply SDK patches outside the lock (they have their own internal lock).
    if patch_openai:
        from veronica_core.patch import patch_openai as _patch_openai
        _patch_openai()

    if patch_anthropic:
        from veronica_core.patch import patch_anthropic as _patch_anthropic
        _patch_anthropic()

    if on_halt != "silent":
        logger.debug(
            "[VERONICA] init: budget=$%.2f max_steps=%d max_retries=%d timeout_ms=%d on_halt=%s",
            max_cost_usd,
            max_steps,
            max_retries_total,
            timeout_ms,
            on_halt,
        )

    # Store on_halt mode on the context object for external inspection.
    # We use a plain attribute rather than a dataclass field so that
    # ExecutionContext itself does not need to be modified.
    _context._quickstart_on_halt = on_halt  # type: ignore[attr-defined]

    return _context


def shutdown() -> None:
    """Tear down the global VERONICA cost-containment layer.

    Cleans up the ExecutionContext created by :func:`init` and removes
    any SDK patches applied during init.

    Safe to call even when :func:`init` has not been called (no-op).
    Safe to call from the atexit handler.
    Thread-safe: protected by the module-level Lock.
    """
    global _context, _initialized

    with _lock:
        if not _initialized:
            return

        ctx = _context
        _context = None
        _initialized = False

    # Close context outside the lock to avoid potential deadlocks with
    # callbacks that might call get_context().
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            logger.debug("[VERONICA] Exception during ExecutionContext cleanup", exc_info=True)

    # Remove SDK patches (idempotent — no-op if nothing was patched).
    try:
        from veronica_core.patch import unpatch_all
        unpatch_all()
    except Exception:
        logger.debug("[VERONICA] Exception during unpatch_all", exc_info=True)

    logger.debug("[VERONICA] shutdown complete")


def get_context() -> Optional[ExecutionContext]:
    """Return the global ExecutionContext, or None if not initialized.

    Thread-safe (read-only access to a module-level reference).

    Returns:
        The active :class:`~veronica_core.containment.ExecutionContext`, or
        ``None`` if :func:`init` has not been called (or after
        :func:`shutdown`).

    Example::

        ctx = veronica_core.get_context()
        if ctx is not None:
            snap = ctx.get_snapshot()
            print(f"Spent: ${snap.cost_usd_accumulated:.4f}")
    """
    return _context
