"""Compliance export module for veronica-core.

Provides async batch export of SafetyEvents and chain snapshots
to a compliance backend (e.g. veronica-risk-audit SaaS).

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
