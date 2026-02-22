"""Auto Cost Estimation for veronica-core (v0.10.0).

Provides pricing lookup and cost estimation for common LLM models.
Estimates are approximate and may differ from actual billing by +/-30%.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Pricing:
    """Per-1K-token pricing in USD for one model.

    Attributes:
        input_per_1k: Cost per 1,000 input (prompt) tokens in USD.
        output_per_1k: Cost per 1,000 output (completion) tokens in USD.
    """

    input_per_1k: float
    output_per_1k: float


# ---------------------------------------------------------------------------
# Pricing table
# Prices as of early 2025. Values are USD per 1K tokens.
# ---------------------------------------------------------------------------

PRICING_TABLE: dict[str, Pricing] = {
    # OpenAI
    "gpt-4o": Pricing(0.005, 0.015),
    "gpt-4o-mini": Pricing(0.000150, 0.000600),
    "gpt-4-turbo": Pricing(0.010, 0.030),
    "gpt-4": Pricing(0.030, 0.060),
    "gpt-3.5-turbo": Pricing(0.0005, 0.0015),
    "o1": Pricing(0.015, 0.060),
    "o1-mini": Pricing(0.003, 0.012),
    "o3": Pricing(0.010, 0.040),
    # Anthropic
    "claude-3-5-sonnet-20241022": Pricing(0.003, 0.015),
    "claude-3-5-haiku-20241022": Pricing(0.001, 0.005),
    "claude-3-opus-20240229": Pricing(0.015, 0.075),
    "claude-opus-4-6": Pricing(0.015, 0.075),
    "claude-sonnet-4-6": Pricing(0.003, 0.015),
    "claude-haiku-4-5-20251001": Pricing(0.001, 0.005),
    # Google
    "gemini-1.5-pro": Pricing(0.00125, 0.005),
    "gemini-1.5-flash": Pricing(0.000075, 0.0003),
    "gemini-2.0-flash": Pricing(0.000075, 0.0003),
}

# Conservative upper-bound fallback for unknown models.
_UNKNOWN_MODEL_FALLBACK = Pricing(0.030, 0.060)


def resolve_model_pricing(model: str) -> Pricing:
    """Return pricing for *model*, falling back gracefully for unknown models.

    Resolution order:
    1. Exact match in PRICING_TABLE.
    2. Prefix match: any key that is a prefix of *model* (longest prefix wins).
    3. Substring match: any key contained in *model*.
    4. _UNKNOWN_MODEL_FALLBACK (conservative upper bound).

    Args:
        model: Model identifier string (e.g. "gpt-4o-2024-11-20").

    Returns:
        Pricing for the matched model or the fallback.
    """
    if not model:
        return _UNKNOWN_MODEL_FALLBACK

    # 1. Exact match
    if model in PRICING_TABLE:
        return PRICING_TABLE[model]

    # 2. Prefix match — find the longest key that is a prefix of model
    prefix_matches = [key for key in PRICING_TABLE if model.startswith(key)]
    if prefix_matches:
        best = max(prefix_matches, key=len)
        return PRICING_TABLE[best]

    # 3. Substring match — key contained in model string
    substring_matches = [key for key in PRICING_TABLE if key in model]
    if substring_matches:
        best = max(substring_matches, key=len)
        return PRICING_TABLE[best]

    return _UNKNOWN_MODEL_FALLBACK


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate API cost in USD for a single LLM call.

    Args:
        model: Model identifier string.
        tokens_in: Number of input (prompt) tokens consumed.
        tokens_out: Number of output (completion) tokens produced.

    Returns:
        Estimated cost in USD (non-negative float).
    """
    if tokens_in == 0 and tokens_out == 0:
        return 0.0
    pricing = resolve_model_pricing(model)
    cost = (tokens_in / 1000.0) * pricing.input_per_1k + (tokens_out / 1000.0) * pricing.output_per_1k
    return cost


def extract_usage_from_response(response: Any) -> tuple[int, int] | None:
    """Extract (input_tokens, output_tokens) from an LLM response object.

    Supports:
    - OpenAI SDK: response.usage.prompt_tokens / completion_tokens
    - Anthropic SDK: response.usage.input_tokens / output_tokens
    - Dict with "usage" key (OpenAI or Anthropic format)
    - Returns None if usage cannot be extracted.

    Args:
        response: LLM response object or dict.

    Returns:
        Tuple of (input_tokens, output_tokens) or None if not extractable.
    """
    if response is None:
        return None

    # Dict-based response (JSON-decoded or mock)
    if isinstance(response, dict):
        usage = response.get("usage")
        if isinstance(usage, dict):
            # OpenAI dict format
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if prompt is not None and completion is not None:
                return int(prompt), int(completion)
            # Anthropic dict format
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if inp is not None and out is not None:
                return int(inp), int(out)
        return None

    # Object-based response (SDK objects)
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    # OpenAI SDK usage object
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is not None and completion is not None:
        return int(prompt), int(completion)

    # Anthropic SDK usage object
    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    if inp is not None and out is not None:
        return int(inp), int(out)

    return None
