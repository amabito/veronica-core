"""Fake exception classes for use in VERONICA demo scenarios.

These are raised inside ``with ctx.llm_call()`` / ``with ctx.tool_call()``
blocks to simulate real-world failure modes without hitting live providers.
"""
from __future__ import annotations


class FakeProviderError(Exception):
    """Simulates a provider returning a 429 (rate-limited) or 500 (server error) response.

    Raise this inside a ``with ctx.llm_call(...)`` block to exercise the
    error-recording and circuit-breaker paths.

    Example::

        with ctx.llm_call(session, model="gpt-4o", ...) as step:
            raise FakeProviderError(status_code=429, message="rate limited")
    """

    def __init__(self, status_code: int = 429, message: str = "") -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(
            f"Provider error {status_code}"
            + (f": {message}" if message else "")
        )


class FakeToolTimeout(Exception):
    """Simulates a tool call that did not return within the expected time window.

    Raise this inside a ``with ctx.tool_call(...)`` block to exercise the
    timeout-recording path.

    Example::

        with ctx.tool_call(session, tool_name="web_search", ...) as step:
            raise FakeToolTimeout()
    """
