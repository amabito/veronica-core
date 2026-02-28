"""Compliance module for veronica-core.

Provides:
- ComplianceExporter: async batch export of SafetyEvents to a compliance backend
- Risk Audit UI: browser-based 9-question agent risk assessment (compliance/app/)

Usage::

    from veronica_core.compliance import ComplianceExporter

    exporter = ComplianceExporter(api_key="vc_live_...")
    exporter.attach(ctx)        # auto-export on context exit
    exporter.flush()            # manual flush
    exporter.close()            # graceful shutdown
"""

from veronica_core.compliance.exporter import ComplianceExporter
from veronica_core.compliance.serializers import (
    serialize_node_record,
    serialize_safety_event,
    serialize_snapshot,
)

__all__ = [
    "ComplianceExporter",
    "serialize_node_record",
    "serialize_safety_event",
    "serialize_snapshot",
]
