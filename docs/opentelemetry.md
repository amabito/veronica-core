# OpenTelemetry Integration (v0.10.0)

VERONICA emits containment events as OpenTelemetry span events. Prompt/response content is **never** exported — only structured metadata.

## Quick Start

```python
from veronica_core.otel import enable_otel

enable_otel(service_name="my-agent")
```

## Exported Attributes

Each OTel span event includes:

| Attribute | Description |
|---|---|
| `veronica.event_type` | e.g. `BUDGET_EXCEEDED`, `EGRESS_BLOCKED` |
| `veronica.decision` | `ALLOW`, `HALT`, `RETRY`, etc. |
| `veronica.hook` | Hook class that fired |
| `veronica.reason` | Human-readable reason (truncated to 500 chars) |
| `veronica.request_id` | Propagated from `ToolCallContext` |

## Custom Exporter

```python
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
enable_otel(service_name="my-agent", exporter=ConsoleSpanExporter())
```

## Privacy Guarantee

The OTel module enforces that no `prompt` or `content` keys appear in exported attributes. Only structural metadata is emitted.

## Install

```bash
pip install "veronica-core[otel]"
```
