"""Demo: InputCompressionHook skeleton (v0.5.0).

Shows decision + evidence behavior without actual compression.
Run: python examples/input_compression_skeleton_demo.py
"""

from veronica_core.shield.input_compression import InputCompressionHook, estimate_tokens
from veronica_core.shield.types import Decision, ToolCallContext

ctx = ToolCallContext(request_id="demo", tool_name="llm")

hook = InputCompressionHook(
    compression_threshold_tokens=500,
    halt_threshold_tokens=1000,
)

print("--- InputCompressionHook demo ---")

# Case 1: Short input (well below threshold)
short_text = "What is the capital of France?" * 3  # ~90 chars -> ~22 tokens
tokens = estimate_tokens(short_text)
result = hook.check_input(short_text, ctx)
label = "ALLOW" if result is None else result.value
print(f"  Short input ({tokens} tokens)  -> {label}")

# Case 2: Medium input (between compress and halt thresholds)
medium_text = "x" * 3000  # 750 tokens -> DEGRADE
tokens = estimate_tokens(medium_text)
result = hook.check_input(medium_text, ctx)
print(f"  Medium input ({tokens} tokens) -> {result.value}  (compression suggested)")

# Case 3: Large input (above halt threshold)
large_text = "x" * 5000  # 1250 tokens -> HALT
tokens = estimate_tokens(large_text)
result = hook.check_input(large_text, ctx)
print(f"  Large input ({tokens} tokens)  -> {result.value}  (input too large)")

# Show evidence from last non-ALLOW check
print()
ev = hook.last_evidence
print("  Evidence (HALT):")
print(f"    estimated_tokens: {ev['estimated_tokens']}")
print(f"    input_sha256: {ev['input_sha256'][:8]}...  (raw text NOT stored)")
print(f"    decision: {ev['decision']}")
