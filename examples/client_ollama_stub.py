"""Example: Ollama-style client stub (no actual HTTP).

Demonstrates how to implement an Ollama-compatible LLM client.
This is a STUB - it does NOT make real HTTP requests.

For real Ollama integration, install `ollama` package and use HTTP client.
"""

from typing import Dict, Any, Optional


class OllamaClientStub:
    """Stub Ollama client for demonstration (no HTTP).

    Real implementation would use:
    - requests or httpx for HTTP calls
    - http://localhost:11434/api/generate endpoint
    - Model parameter (e.g., "llama3.2:3b")

    This stub returns canned responses for testing.
    """

    def __init__(self, model: str = "llama3.2:3b"):
        """Initialize stub client.

        Args:
            model: Model name (unused in stub)
        """
        self.model = model
        self.base_url = "http://localhost:11434"  # Not used (stub)

    def generate(
        self,
        prompt: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ) -> str:
        """Generate response (stub - returns canned text).

        Real implementation would:
        ```python
        import requests
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False}
        )
        return response.json()["response"]
        ```

        Returns:
            Canned response (stub)
        """
        # Stub logic: Simple keyword matching
        prompt_lower = prompt.lower()

        if "safe" in prompt_lower or "risk" in prompt_lower:
            return "SAFE"
        elif "error" in prompt_lower or "fail" in prompt_lower:
            return "UNSAFE"
        else:
            return "NEUTRAL"


def main():
    """Demonstrate Ollama stub usage with VERONICA."""
    from veronica_core import VeronicaIntegration

    # Initialize with Ollama stub
    client = OllamaClientStub(model="llama3.2:3b")

    veronica = VeronicaIntegration(
        cooldown_fails=3,
        cooldown_seconds=60,
        client=client,
    )

    print("=== VERONICA with OllamaClientStub ===\n")
    print(f"Model: {client.model}")
    print(f"Base URL: {client.base_url} (not used - stub)\n")

    # Test 1: Safe operation
    prompt1 = "Is database backup safe to run?"
    response1 = veronica.client.generate(prompt1)
    print(f"Prompt: {prompt1}")
    print(f"Response: {response1}\n")

    # Test 2: Risky operation
    prompt2 = "Should we proceed despite error rate > 50%?"
    response2 = veronica.client.generate(prompt2)
    print(f"Prompt: {prompt2}")
    print(f"Response: {response2}\n")

    print("NOTE: This is a STUB. For real Ollama integration:")
    print("1. Install: pip install requests")
    print("2. Start Ollama: ollama serve")
    print("3. Replace stub logic with HTTP POST to /api/generate")


if __name__ == "__main__":
    main()
