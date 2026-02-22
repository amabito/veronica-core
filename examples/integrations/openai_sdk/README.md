# VERONICA Core + OpenAI SDK (Transparent Patch)

Enforce VERONICA budget and step limits on OpenAI / Anthropic SDK calls
**without touching a single call site**.

## How it works

```
patch_openai()   # wrap Completions.create once at startup

@veronica_guard(max_cost_usd=1.0, max_steps=50)
def my_agent():
    # existing code — completely unchanged
    client.chat.completions.create(model="gpt-4o-mini", messages=[...])
    #                          ^--- now guarded automatically
```

The patch is a **no-op outside a `veronica_guard` boundary** — library code
and one-off scripts remain unaffected.

## Quick start

```python
from veronica_core import patch_openai, unpatch_all, veronica_guard
from veronica_core.inject import VeronicaHalt

# 1. Activate at startup (idempotent, thread-safe)
patch_openai()

# 2. Declare limits on the function that owns the session
@veronica_guard(max_cost_usd=0.50, max_steps=20)
def run_session(question: str) -> str:
    import openai
    client = openai.OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
    )
    return resp.choices[0].message.content

try:
    print(run_session("What is VERONICA?"))
except VeronicaHalt as e:
    print(f"Session halted: {e}")
finally:
    unpatch_all()  # restore SDK at program exit
```

## Anthropic

```python
from veronica_core import patch_anthropic, unpatch_all, veronica_guard

patch_anthropic()

@veronica_guard(max_cost_usd=0.50)
def ask_claude(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
```

## Run the demo

```bash
uv run python examples/integrations/openai_sdk/example.py
```

No API key required — uses mock responses.

## Demos included

1. **Transparent enforcement** — patch + guard halts second call via budget
2. **Outside-guard passthrough** — unguarded calls are never blocked
3. **Idempotency** — multiple `patch_openai()` calls are safe

## Properties

| Property | Value |
|----------|-------|
| Thread-safe | Yes (`threading.Lock` on patch registry) |
| Idempotent | Yes (double-patch is a no-op) |
| Outside-guard behavior | Pass-through (no enforcement) |
| Supported SDKs | `openai` v1.x, `openai` v0.x (legacy), `anthropic` |
