# VERONICA Core + LangChain

Plug VERONICA policy enforcement into any LangChain pipeline via the standard
`BaseCallbackHandler` interface — no call-site changes required.

## What it does

`VeronicaCallbackHandler` intercepts every LLM invocation:

| Hook | Action |
|------|--------|
| `on_llm_start` | Policy check — raises `VeronicaHalt` if denied |
| `on_llm_end` | Records token cost against `BudgetEnforcer` |
| `on_llm_error` | Logs error without charging budget |

## Quick start

```python
from langchain_openai import ChatOpenAI
from veronica_core import GuardConfig
from veronica_core.adapters.langchain import VeronicaCallbackHandler
from veronica_core.inject import VeronicaHalt

handler = VeronicaCallbackHandler(GuardConfig(
    max_cost_usd=1.0,   # hard cost ceiling
    max_steps=20,        # max LLM calls per session
))

llm = ChatOpenAI(model="gpt-4o-mini", callbacks=[handler])

try:
    result = llm.invoke("Explain VERONICA in one sentence.")
    print(result.content)
except VeronicaHalt as e:
    print(f"Blocked: {e}")
```

## Run the demo

```bash
# From the project root
uv run python examples/integrations/langchain/example.py
```

The demo uses a stub LLM — no API key required.

## Demos included

1. **Step limit** — 3-call cap; 4th call raises `VeronicaHalt`
2. **Budget limit** — $0.0015 cap; halts after budget is exhausted
3. **Introspection** — read `handler.container` for live spend/step metrics

## Requirements

```
pip install langchain-core
```

OpenAI/Anthropic are optional — swap `ChatOpenAI` for any `BaseLanguageModel`.
