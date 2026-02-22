# Auto Cost Estimation (v0.10.0)

veronica-core v0.10.0 introduces automatic cost estimation for LLM calls.
Previously, callers had to provide `cost_estimate_hint` manually.
Now, pass `model` and `response_hint` to `WrapOptions` and the cost is computed automatically.

## Purpose

Eliminate manual bookkeeping of per-call costs. The pricing module resolves
model pricing and computes USD cost from actual token usage reported by the SDK.

## Usage

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext, WrapOptions

config = ExecutionConfig(max_cost_usd=1.0, max_steps=50, max_retries_total=10)

with ExecutionContext(config=config) as ctx:
    response = client.chat.completions.create(model="gpt-4o", messages=[...])
    ctx.wrap_llm_call(
        fn=lambda: None,
        options=WrapOptions(model="gpt-4o", response_hint=response),
    )
    snap = ctx.get_snapshot()
    print(f"Cost so far: ${snap.cost_usd_accumulated:.4f}")
```

## Supported Models

### OpenAI
| Model | Input (per 1K) | Output (per 1K) |
|-------|---------------|----------------|
| gpt-4o | $0.0050 | $0.0150 |
| gpt-4o-mini | $0.000150 | $0.000600 |
| gpt-4-turbo | $0.010 | $0.030 |
| gpt-4 | $0.030 | $0.060 |
| gpt-3.5-turbo | $0.0005 | $0.0015 |
| o1 | $0.015 | $0.060 |
| o1-mini | $0.003 | $0.012 |
| o3 | $0.010 | $0.040 |

### Anthropic
| Model | Input (per 1K) | Output (per 1K) |
|-------|---------------|----------------|
| claude-3-5-sonnet-20241022 | $0.003 | $0.015 |
| claude-3-5-haiku-20241022 | $0.001 | $0.005 |
| claude-3-opus-20240229 | $0.015 | $0.075 |
| claude-opus-4-6 | $0.015 | $0.075 |
| claude-sonnet-4-6 | $0.003 | $0.015 |
| claude-haiku-4-5-20251001 | $0.001 | $0.005 |

### Google
| Model | Input (per 1K) | Output (per 1K) |
|-------|---------------|----------------|
| gemini-1.5-pro | $0.00125 | $0.005 |
| gemini-1.5-flash | $0.000075 | $0.0003 |
| gemini-2.0-flash | $0.000075 | $0.0003 |

## Fallback Behavior for Unknown Models

If the model name is not in the pricing table, `resolve_model_pricing` applies
a conservative upper-bound fallback:

- Input: $0.030 / 1K tokens
- Output: $0.060 / 1K tokens

Resolution order:
1. Exact match
2. Prefix match (e.g. `"gpt-4o-2024-11-20"` matches `"gpt-4o"`)
3. Substring match
4. Fallback

## Warning: No response_hint

If `model` is set but `response_hint` is omitted, veronica-core cannot extract
token usage and records `cost_usd=0.0`. A `SafetyEvent` with
`event_type="COST_ESTIMATION_SKIPPED"` is emitted to the chain event log.

```python
# This will emit a COST_ESTIMATION_SKIPPED event
WrapOptions(model="gpt-4o")  # no response_hint

# This correctly computes cost
WrapOptions(model="gpt-4o", response_hint=response)
```

## Accuracy Note

Cost estimates are **approximate** and may differ from actual billing by +/-30%
due to:
- Pricing table staleness (models may change pricing)
- Cached token discounts not reflected
- Batch API discounts not reflected
- Regional pricing differences

For exact billing, refer to your provider's dashboard.

## Direct API

```python
from veronica_core.pricing import (
    Pricing,
    PRICING_TABLE,
    resolve_model_pricing,
    estimate_cost_usd,
    extract_usage_from_response,
)

# Lookup pricing
pricing = resolve_model_pricing("gpt-4o")
print(pricing.input_per_1k)   # 0.005
print(pricing.output_per_1k)  # 0.015

# Estimate cost directly
cost = estimate_cost_usd("gpt-4o", tokens_in=1000, tokens_out=500)
print(f"${cost:.4f}")  # $0.0125

# Extract usage from response
usage = extract_usage_from_response(response)  # (input_tokens, output_tokens)
```
