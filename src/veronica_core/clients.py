"""VERONICA LLM Client Interface - Pluggable AI integration (optional).

This module defines the protocol for LLM client integration.
VERONICA Core does NOT require LLM - this is purely optional.
"""

from __future__ import annotations
from typing import Protocol, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Protocol for pluggable LLM client integration.

    VERONICA Core does NOT depend on LLM functionality.
    This protocol enables optional AI-enhanced decision logic.

    Example implementations:
    - Ollama client (llama3.2:3b)
    - OpenAI client (GPT-4)
    - Anthropic client (Claude)
    - Gemini client
    - Dummy/mock client for testing

    Implementation must be thread-safe for production use.
    """

    def generate(
        self,
        prompt: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ) -> str:
        """Generate text response from LLM.

        Args:
            prompt: Input prompt text
            context: Optional context data (entity info, history, etc.)
            **kwargs: Implementation-specific parameters (model, temperature, etc.)

        Returns:
            Generated text response

        Raises:
            Exception: On generation failure (network, rate limit, etc.)
        """
        ...


class NullClient:
    """Null LLM client that safely fails when invoked.

    Use this as default to ensure VERONICA Core works without LLM.
    Raises clear error if LLM functionality is used without client injection.
    """

    def generate(
        self,
        prompt: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ) -> str:
        """Raise error - LLM client not configured."""
        raise RuntimeError(
            "LLMClient not configured. "
            "Pass client= to VeronicaIntegration() to enable LLM features."
        )


class DummyClient:
    """Dummy LLM client for testing (returns fixed responses).

    Use this for unit tests that need LLM without external dependencies.
    """

    def __init__(self, fixed_response: str = "OK"):
        """Initialize with fixed response.

        Args:
            fixed_response: Response to return for all prompts
        """
        self.fixed_response = fixed_response
        self.call_count = 0
        self.last_prompt: Optional[str] = None

    def generate(
        self,
        prompt: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ) -> str:
        """Return fixed response (for testing)."""
        self.call_count += 1
        self.last_prompt = prompt
        logger.debug(f"[DummyClient] Call #{self.call_count}: {prompt[:50]}...")
        return self.fixed_response
