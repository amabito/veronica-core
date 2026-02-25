# VERONICA Core — Integration Showcases

Four minimal, self-contained examples showing how VERONICA plugs into
real LLM infrastructure. Each runs without an API key.

| Folder | Integration | What it shows |
|--------|-------------|---------------|
| [`langchain/`](langchain/) | `VeronicaCallbackHandler` | Drop into any LangChain LLM via callbacks |
| [`openai_sdk/`](openai_sdk/) | `patch_openai()` | Transparent enforcement, zero call-site changes |
| [`decorator/`](decorator/) | `@veronica_guard` | Standalone decorator, no framework needed |
| [`autogen/`](autogen/) | `VeronicaIntegration` | Circuit breaker for AG2 agents |

## Run all demos

```bash
# From the project root
uv run python examples/integrations/langchain/example.py
uv run python examples/integrations/openai_sdk/example.py
uv run python examples/integrations/decorator/example.py
uv run python examples/integrations/autogen/example.py
```

## Which integration should I use?

```
Using LangChain?          -> langchain/   (VeronicaCallbackHandler)
Using OpenAI/Anthropic SDK
  without LangChain?      -> openai_sdk/  (patch_openai / patch_anthropic)
No framework / custom LLM -> decorator/   (@veronica_guard)
Using AG2?                -> autogen/     (VeronicaIntegration circuit breaker)
```

All three can be combined: patch once at startup, add `@veronica_guard` on
agent entry points, and attach `VeronicaCallbackHandler` to LangChain chains
— the limits are additive.
