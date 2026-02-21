# Catchup Pattern

Standard approach for handling missed or failed pipeline runs across the
workflow platform.

## Two Models

### Pull-based pipelines (scrapers, ETL)

**Pattern**: Watermark -- track the last successful execution point and
process forward from there on the next run.

**How it works**:

1. Before each run, query the pipeline's run-log table for the most recent
   successful execution timestamp.
2. Use that timestamp as the starting point for the current run.
3. If no successful run exists, fall back to a sensible default (e.g., 7 days
   back for ETL, "scrape everything" for scrapers).
4. Process all records from the watermark to now.
5. On success, the new run becomes the watermark for next time.

**Idempotency requirement**: The pipeline must use upserts (INSERT ... ON
CONFLICT UPDATE) so that reprocessing the same records is safe. This is what
makes catchup a no-risk operation.

**Reference implementation**: DefenderShield ETL `--catchup` mode
(`daily_runner.py`). Key behaviors:

- Bronze: watermark-based ingestion from SkuVault API
- Silver: processes ALL orders with no date filter (sidesteps date alignment
  bugs after multi-day gaps)
- Gold: truncate-and-reload from silver (fast, always consistent)
- Self-heals on next cron run if today's run fails

### Push-based services (webhooks, event listeners)

**Pattern**: Log gaps and alert. Missed events cannot be replayed because the
source does not offer historical queries.

**How it works**:

1. Track the last received event timestamp.
2. On each run (or periodic check), compare against expected cadence.
3. If the gap exceeds a threshold, send a warning via workflow-notify.
4. Human investigates -- the fix may require contacting the upstream source or
   accepting the data gap.

## Gap Detection

Every pipeline with a run-log should include a gap detection check:

- Query the run-log for the last successful run.
- If older than `threshold` (default: 36 hours for daily pipelines), fire a
  warning notification via `workflow-notify.fanout()`.
- Threshold = 1.5x the expected cadence (daily pipeline -> 36h, hourly -> 90m).

The gap check can be triggered by:

1. The pipeline itself at the start of each run (self-check).
2. A separate cron job (independent monitoring).
3. The behavioral auditor in prod mode (Phase 4).

## Anti-patterns

- **Silent skip**: Pipeline detects a gap but processes only the current
  window, silently dropping the missed period. This is the primary failure
  mode this pattern prevents.
- **Unbounded replay**: Processing years of historical data on a single
  catchup run, overwhelming the target or source. Use reasonable defaults
  (7-30 day lookback caps).
- **No idempotency**: If the pipeline uses INSERT without ON CONFLICT, catchup
  will create duplicates. Fix the schema first.
