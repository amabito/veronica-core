# METRICS.md Quick Start

**3-minute guide to reproducing VERONICA Core metrics**

## Step 1: Generate Sample Log

```bash
python scripts/metrics_aggregate.py --sample
```

This creates `data/logs/sample.log` with 20 entries from METRICS.md.

## Step 2: Aggregate Metrics

```bash
python scripts/metrics_aggregate.py data/logs/sample.log
```

**Expected output:**
```
VERONICA CORE - PRODUCTION METRICS
======================================================================
Log file: data\logs\sample.log
Duration: 0.0 days (19.0 seconds)

Operations/second: 0.6 ops/sec
Total operations: 11 ops
Crashes handled: 2 crashes
Recovery rate: 100.0% (2/2 successful)
Data loss: 2 operations (18.182% of total)

======================================================================
[VERDICT] Acceptable reliability (minor data loss)
======================================================================
```

## Step 3: Understanding Metrics

- **ops/sec**: 11 operations / 19 seconds = 0.6 ops/sec
- **total_ops**: 11 log entries with `event_type=operation`
- **crashes_handled**: 2 crashes (SIGKILL, SIGTERM) with successful recovery
- **recovery_rate**: 2 recoveries / 2 crashes = 100%
- **data_loss**: Estimated from checkpoint intervals (time between last checkpoint and crash)

## Next Steps

1. **Read full documentation**: `docs/METRICS.md`
2. **Integrate into your system**: Add operation logging to your application
3. **Run for 30 days**: Let metrics accumulate in production
4. **Aggregate and analyze**: Use `metrics_aggregate.py` to compute final metrics

## Sample Log Format

Your application logs should follow this CSV format:

```csv
timestamp,event_type,entity_id,status,detail
1771200000.0,checkpoint,system,saved,checksum_a1b2c3
1771200001.0,operation,btc_jpy,success,price_scan
1771200003.0,operation,xrp_jpy,fail,timeout
1771200007.0,crash,system,SIGKILL,9
1771200008.0,recovery,system,success,state_restored
```

See `docs/METRICS.md` for detailed log format specification.
