"""VERONICA compliance export demo -- SafetyEvent batch export to SaaS backend.

Demonstrates the ComplianceExporter workflow:
  1. Create an ExecutionContext with budget + step limits
  2. Attach the ComplianceExporter for automatic snapshot export
  3. Run a simulated agent loop that triggers HALT
  4. Inspect the serialized payload that would be sent

No real HTTP calls are made -- the demo intercepts _send_one to print
the payload instead of posting to a remote endpoint.

Run:
    pip install -e .
    python examples/compliance_export_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running from repo root without installing.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from veronica_core.compliance import ComplianceExporter
from veronica_core.containment import (
    ChainMetadata,
    ExecutionConfig,
    ExecutionContext,
    WrapOptions,
)
from veronica_core.shield.types import Decision

SEPARATOR = "-" * 60


def main() -> None:
    print("VERONICA Compliance Export Demo")
    print("=" * 60)
    print()

    # -- 1. Set up containment limits -----------------------------------

    config = ExecutionConfig(
        max_cost_usd=0.10,
        max_steps=5,
        max_retries_total=10,
        timeout_ms=0,
    )
    meta = ChainMetadata(
        request_id="req-demo-001",
        chain_id="chain-demo-001",
        org_id="acme-corp",
        team="ml-platform",
        service="summariser",
        model="gpt-4o",
        tags={"env": "demo", "feature": "compliance"},
    )

    # -- 2. Create exporter (intercept HTTP for demo) -------------------

    captured_payloads: list[dict] = []

    exporter = ComplianceExporter(
        api_key="vc_demo_not_a_real_key",
        endpoint="https://audit.veronica-core.dev/api/ingest",
        flush_interval_s=0.5,
    )

    # Monkey-patch _send_one to capture instead of HTTP POST
    original_send = exporter._send_one

    def _capture_send(payload: dict) -> None:
        captured_payloads.append(payload)
        print(f"  [CAPTURED] Payload for chain={payload['chain']['chain_id']}")

    exporter._send_one = _capture_send  # type: ignore[assignment]

    # -- 3. Run agent loop with exporter attached -----------------------

    print(SEPARATOR)
    print("Running agent loop (budget=$0.10, max_steps=5)")
    print(SEPARATOR)
    print()

    with ExecutionContext(config=config, metadata=meta) as ctx:
        exporter.attach(ctx)

        call_count = 0
        for i in range(20):
            decision = ctx.wrap_llm_call(
                fn=lambda: f"simulated response {i}",
                options=WrapOptions(
                    operation_name=f"agent_step_{i}",
                    cost_estimate_hint=0.02,
                ),
            )
            call_count += 1
            status = decision.name
            snap = ctx.get_snapshot()
            print(
                f"  Step {call_count}: ${0.02:.2f} -> {status}"
                f"  (total: ${snap.cost_usd_accumulated:.2f},"
                f" steps: {snap.step_count})"
            )

            if decision == Decision.HALT:
                break

    # -- 4. Flush and inspect -------------------------------------------

    exporter.flush()
    exporter.close()

    print()
    print(SEPARATOR)
    print("Exported payload")
    print(SEPARATOR)
    print()

    if not captured_payloads:
        print("  [ERROR] No payloads captured -- attach() did not fire.")
        sys.exit(1)

    for i, payload in enumerate(captured_payloads):
        print(f"Payload #{i + 1}:")
        print()

        chain = payload["chain"]
        print("  Chain summary:")
        print(f"    chain_id:    {chain['chain_id']}")
        print(f"    request_id:  {chain['request_id']}")
        print(f"    service:     {chain.get('service', 'N/A')}")
        print(f"    team:        {chain.get('team', 'N/A')}")
        print(f"    model:       {chain.get('model', 'N/A')}")
        print(f"    steps:       {chain['step_count']}")
        print(f"    cost:        ${chain['cost_usd']:.4f}")
        print(f"    retries:     {chain['retries_used']}")
        print(f"    aborted:     {chain['aborted']}")
        print(f"    elapsed_ms:  {chain['elapsed_ms']:.1f}")
        print(f"    tags:        {chain.get('tags', {})}")

        if "graph_summary" in chain and chain["graph_summary"]:
            gs = chain["graph_summary"]
            print(f"    graph:       llm={gs.get('total_llm_calls', 0)},"
                  f" cost=${gs.get('total_cost_usd', 0):.4f}")

        events = payload["events"]
        print()
        print(f"  Safety events ({len(events)}):")
        if events:
            for ev in events:
                print(
                    f"    [{ev['decision']}] {ev['event_type']}"
                    f" -- {ev['reason']}"
                )
        else:
            print("    (none -- all calls were ALLOW)")

    # -- 5. Show raw JSON -----------------------------------------------

    print()
    print(SEPARATOR)
    print("Raw JSON (what POST /api/ingest receives)")
    print(SEPARATOR)
    print()
    print(json.dumps(captured_payloads[0], indent=2, default=str))

    print()
    print("=" * 60)
    print("This payload is what veronica-compliance SaaS stores.")
    print("Dashboard, audit trail, and EU AI Act reports are")
    print("generated from this data.")
    print()
    print("  POST /api/ingest")
    print("  Authorization: Bearer vc_live_...")
    print("  Content-Type: application/json")
    print("  Body: <above JSON>")
    print("=" * 60)


if __name__ == "__main__":
    main()
