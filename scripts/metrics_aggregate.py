#!/usr/bin/env python3
"""VERONICA Core - Metrics Aggregation Script

Computes production metrics from operation logs using only Python stdlib.

Usage:
    python metrics_aggregate.py <log_file>

Example:
    python metrics_aggregate.py data/logs/operations.log

Log Format (CSV):
    timestamp,event_type,entity_id,status,detail

Metrics Computed:
    - ops/sec: Operations per second (throughput)
    - total_ops: Total operation count
    - crashes_handled: Number of crash events with successful recovery
    - recovery_rate: Percentage of successful recoveries
    - data_loss: Operations lost between checkpoints and crashes

See docs/METRICS.md for detailed metric definitions.
"""

import sys
import csv
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class LogEntry:
    """Parsed log entry."""

    timestamp: float
    event_type: str
    entity_id: str
    status: str
    detail: str


@dataclass
class MetricsResult:
    """Aggregated metrics."""

    ops_per_sec: float = 0.0
    total_ops: int = 0
    crashes_handled: int = 0
    recovery_rate: float = 0.0
    data_loss: int = 0
    duration_seconds: float = 0.0
    total_crashes: int = 0
    successful_recoveries: int = 0
    parse_errors: int = 0
    last_checkpoint_time: Dict[str, float] = field(default_factory=dict)


class MetricsAggregator:
    """Aggregates production metrics from VERONICA operation logs."""

    def __init__(self, log_path: Path):
        """Initialize aggregator.

        Args:
            log_path: Path to operation log file (CSV format)
        """
        self.log_path = log_path
        self.entries: List[LogEntry] = []
        self.result = MetricsResult()

    def parse_log_line(self, line: str) -> Optional[LogEntry]:
        """Parse a single CSV log line.

        Args:
            line: CSV line in format: timestamp,event_type,entity_id,status,detail

        Returns:
            LogEntry if valid, None if malformed
        """
        try:
            parts = line.strip().split(",")
            if len(parts) < 4:
                return None

            timestamp = float(parts[0])
            event_type = parts[1]
            entity_id = parts[2]
            status = parts[3]
            detail = parts[4] if len(parts) > 4 else ""

            return LogEntry(
                timestamp=timestamp,
                event_type=event_type,
                entity_id=entity_id,
                status=status,
                detail=detail,
            )
        except (ValueError, IndexError):
            return None

    def load_log(self) -> bool:
        """Load and parse log file.

        Returns:
            True if log loaded successfully, False otherwise
        """
        if not self.log_path.exists():
            print(f"[ERROR] Log file not found: {self.log_path}")
            return False

        try:
            with open(self.log_path, "r") as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    # Skip header if present
                    if i == 0 and row[0] == "timestamp":
                        continue

                    line = ",".join(row)
                    entry = self.parse_log_line(line)

                    if entry:
                        self.entries.append(entry)
                    else:
                        self.result.parse_errors += 1

            if self.result.parse_errors > 0:
                print(
                    f"[WARNING] Skipped {self.result.parse_errors} malformed lines"
                )

            return True

        except Exception as e:
            print(f"[ERROR] Failed to load log file: {e}")
            return False

    def compute_metrics(self) -> MetricsResult:
        """Compute all metrics from loaded log entries.

        Returns:
            MetricsResult with all computed metrics
        """
        if not self.entries:
            print("[WARNING] No valid log entries found")
            return self.result

        # Sort by timestamp
        self.entries.sort(key=lambda e: e.timestamp)

        first_ts = self.entries[0].timestamp
        last_ts = self.entries[-1].timestamp
        self.result.duration_seconds = last_ts - first_ts

        # Track checkpoints per entity (for data loss calculation)
        last_checkpoint: Dict[str, float] = {}
        crash_times: List[float] = []
        recovery_times: List[float] = []

        for entry in self.entries:
            # Count operations (success or fail)
            if entry.event_type == "operation" and entry.status in (
                "success",
                "fail",
            ):
                self.result.total_ops += 1

            # Track checkpoints
            elif entry.event_type == "checkpoint" and entry.status == "saved":
                last_checkpoint[entry.entity_id] = entry.timestamp

            # Track crashes
            elif entry.event_type == "crash":
                self.result.total_crashes += 1
                crash_times.append(entry.timestamp)

                # Calculate data loss for this crash
                if entry.entity_id in last_checkpoint:
                    time_since_checkpoint = (
                        entry.timestamp - last_checkpoint[entry.entity_id]
                    )
                    # Estimate operations lost (assuming uniform rate)
                    if self.result.duration_seconds > 0:
                        ops_rate = (
                            self.result.total_ops
                            / self.result.duration_seconds
                        )
                        lost_ops = int(time_since_checkpoint * ops_rate)
                        self.result.data_loss += lost_ops

            # Track recoveries
            elif entry.event_type == "recovery" and entry.status == "success":
                self.result.successful_recoveries += 1
                recovery_times.append(entry.timestamp)

        # Compute derived metrics
        if self.result.duration_seconds > 0:
            self.result.ops_per_sec = (
                self.result.total_ops / self.result.duration_seconds
            )

        # crashes_handled = crashes with successful recovery
        # We assume each crash is followed by a recovery attempt
        self.result.crashes_handled = min(
            self.result.total_crashes, self.result.successful_recoveries
        )

        if self.result.total_crashes > 0:
            self.result.recovery_rate = (
                self.result.successful_recoveries / self.result.total_crashes
            ) * 100
        else:
            # No crashes = trivial 100% recovery rate
            self.result.recovery_rate = 100.0

        return self.result

    def print_metrics(self) -> None:
        """Print formatted metrics table."""
        print("\nVERONICA CORE - PRODUCTION METRICS")
        print("=" * 70)
        print(f"Log file: {self.log_path}")

        if self.result.duration_seconds > 0:
            duration_days = self.result.duration_seconds / 86400
            print(
                f"Duration: {duration_days:.1f} days ({self.result.duration_seconds:.1f} seconds)"
            )
        else:
            print("Duration: N/A (empty log)")

        print()

        # Operations
        print(f"Operations/second: {self.result.ops_per_sec:.1f} ops/sec")
        print(f"Total operations: {self.result.total_ops:,} ops")

        # Crashes
        print(f"Crashes handled: {self.result.crashes_handled} crashes")
        print(
            f"Recovery rate: {self.result.recovery_rate:.1f}% "
            f"({self.result.successful_recoveries}/{self.result.total_crashes} successful)"
        )

        # Data loss
        if self.result.total_ops > 0:
            loss_pct = (self.result.data_loss / self.result.total_ops) * 100
            print(
                f"Data loss: {self.result.data_loss:,} operations ({loss_pct:.3f}% of total)"
            )
        else:
            print("Data loss: N/A (no operations)")

        print()
        print("=" * 70)

        # Verdict
        if self.result.total_ops == 0 and self.result.total_crashes == 0:
            print("[VERDICT] No data (empty log)")
        elif self.result.recovery_rate >= 99.0 and (
            self.result.total_ops == 0
            or (self.result.data_loss / self.result.total_ops) < 0.001
        ):
            print("[VERDICT] Production-grade reliability")
        elif self.result.recovery_rate >= 95.0:
            print("[VERDICT] Acceptable reliability (minor data loss)")
        else:
            print(
                "[VERDICT] Requires improvement (low recovery rate or high data loss)"
            )

        print("=" * 70)


def print_usage() -> None:
    """Print usage instructions."""
    print("Usage: python metrics_aggregate.py <log_file>")
    print()
    print("Example:")
    print("  python metrics_aggregate.py data/logs/operations.log")
    print()
    print("Log format (CSV):")
    print("  timestamp,event_type,entity_id,status,detail")
    print()
    print("See docs/METRICS.md for detailed documentation")


def main() -> int:
    """Main entry point.

    Returns:
        0 if successful, 1 if error
    """
    if len(sys.argv) < 2:
        print("[ERROR] Missing log file argument")
        print()
        print_usage()
        return 1

    log_path = Path(sys.argv[1])

    # Handle sample log generation
    if log_path.name == "--sample":
        print("Generating sample log: data/logs/sample.log")
        generate_sample_log(Path("data/logs/sample.log"))
        print("[OK] Sample log generated")
        print()
        print("Run: python metrics_aggregate.py data/logs/sample.log")
        return 0

    # Aggregate metrics
    aggregator = MetricsAggregator(log_path)

    if not aggregator.load_log():
        return 1

    aggregator.compute_metrics()
    aggregator.print_metrics()

    return 0


def generate_sample_log(output_path: Path) -> None:
    """Generate sample log file for testing.

    Args:
        output_path: Path to write sample log
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Sample log from METRICS.md
    sample_log = """timestamp,event_type,entity_id,status,detail
1771200000.0,checkpoint,system,saved,checksum_a1b2c3
1771200001.0,operation,btc_jpy,success,price_scan
1771200002.0,operation,eth_jpy,success,price_scan
1771200003.0,operation,xrp_jpy,fail,timeout
1771200004.0,operation,btc_jpy,success,trade_executed
1771200005.0,operation,eth_jpy,fail,insufficient_balance
1771200006.0,operation,xrp_jpy,success,trade_executed
1771200007.0,crash,system,SIGKILL,9
1771200008.0,recovery,system,success,state_restored
1771200009.0,checkpoint,system,saved,checksum_d4e5f6
1771200010.0,operation,btc_jpy,success,price_scan
1771200011.0,operation,eth_jpy,success,price_scan
1771200012.0,state_transition,system,SAFE_MODE,manual_halt
1771200013.0,checkpoint,system,saved,checksum_g7h8i9
1771200014.0,crash,system,SIGTERM,15
1771200015.0,recovery,system,success,state_restored
1771200016.0,operation,btc_jpy,success,price_scan
1771200017.0,operation,eth_jpy,fail,api_rate_limit
1771200018.0,operation,xrp_jpy,success,trade_executed
1771200019.0,checkpoint,system,saved,checksum_j1k2l3
"""

    with open(output_path, "w") as f:
        f.write(sample_log)


if __name__ == "__main__":
    sys.exit(main())
