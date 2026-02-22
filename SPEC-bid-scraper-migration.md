# Bid Scraper Migration to Workflow Platform

## System Overview

Migrate the bid-scraper service from its legacy orchestration layer (`run-and-audit.sh` shell script with raw Discord webhook) to the `workflow-orchestrate monitor --exec` CLI. The scraper container, Postgres database, Dokploy compose, and scraping logic remain unchanged. Only the cron wrapper and notification routing change. The `--exec` infrastructure already exists from the ETL migration -- this is a crontab swap, spec/access doc updates, and retirement of `run-and-audit.sh`.

## Behavioral Contract

### Service Execution

- When `workflow-orchestrate monitor --service bid-scraper --exec "python -m bid_scraper run"` is invoked, the system executes the scraper via `docker exec` against the `compose-bypass-solid-state-feed-6p6e3c-scraper-1` container, then runs the behavioral audit.
- When the scraper command succeeds (exit 0), the system proceeds to the audit phase.
- When the scraper command fails (exit non-zero, e.g., Bonfire portal timeout), the system logs the failure, sends a `warning` notification, and still proceeds to the audit phase.

### Container Availability

- When the scraper container is not running, the system detects this before attempting `docker exec`, sends a `critical` severity notification, and exits without running the audit.

### Audit Phase

- When the audit completes, the report is archived to `~/audit-reports/bid-scraper/prod_{timestamp}/` alongside `exec_output.log`.
- When the auditor exceeds the 10-minute timeout, the auditor container is killed and an `error` report is produced.

### Notification Routing

- When any phase produces a result, notifications route through `workflow-notify.fanout()`:
  - Audit `pass` -> `success` (Discord only)
  - Audit `fail` -> `warning` (Discord + Vault)
  - Audit `error` (auditor failure/timeout) -> `warning` (Discord + Vault)
  - Exec failure -> `warning` (Discord + Vault)
  - Container not running -> `critical` (Discord + Vault + Email)
- When the bid-scraper's own legacy email notification fires (Gmail SMTP), the system does not interfere. Both notification paths coexist.

### Schedule

- When the migration is complete, the host crontab entry at 5:00 AM ET (10:00 UTC) calls `workflow-orchestrate monitor --exec ...` instead of `run-and-audit.sh bid-scraper`.

### Spec Updates

- When the access doc references the service schedule, it reflects 5:00 AM ET via `workflow-orchestrate monitor --exec`.

## Explicit Non-Behaviors

- The system must not modify the bid-scraper container, its image, its compose file, or its Dokploy configuration, because the scraper itself is not changing.
- The system must not modify the bid-scraper's internal email notification logic, because legacy email coexists with `workflow-notify` intentionally.
- The system must not change the behavioral spec scenario thresholds (freshness at 72 hours, success rate at 80%, etc.), because those thresholds are appropriate for a daily-scheduled scraper.
- The system must not add any new code to `orchestrate.py` or `config.py`, because the `--exec` infrastructure already handles bid-scraper (container mapping already exists in `service_containers`).
- The system must not retry a failed scraper exec, because a transient portal failure will resolve on the next daily run.
- The system must not keep `run-and-audit.sh` on disk, because bid-scraper was the last consumer and the script is now fully replaced.

## Integration Boundaries

All integration boundaries are identical to the ETL migration (Docker Engine, workflow-notify, auditor container, host crontab). No new integrations are introduced. See `SPEC-etl-migration.md` for details.

## Behavioral Scenarios

### Happy Path

**Scenario 1: Full successful run**
GIVEN the bid-scraper container is running and healthy.
WHEN `workflow-orchestrate monitor --service bid-scraper --exec "python -m bid_scraper run" --spec specs/bid-scraper.md --access specs/bid-scraper-access.md` is invoked.
THEN the scraper runs to completion, the auditor evaluates all 10 behavioral scenarios, the report and exec output are archived to `~/audit-reports/bid-scraper/prod_*/`, and a notification is sent via `workflow-notify`.

**Scenario 2: Notification routing on pass**
GIVEN the scraper ran successfully and all 10 audit scenarios pass.
WHEN the monitor command completes.
THEN a `success` severity notification is sent to Discord only.

**Scenario 3: Exec fails but audit still runs**
GIVEN the bid-scraper container is running but the scraper exits non-zero (e.g., Bonfire portal 503).
WHEN the monitor command processes the exec failure.
THEN a `warning` notification is sent about the exec failure, and the auditor still runs to evaluate existing data quality.

### Error Paths

**Scenario 4: Container not running**
GIVEN the bid-scraper container is stopped or does not exist.
WHEN the monitor command attempts to execute with `--exec`.
THEN a `critical` notification fires, no exec or audit runs, and the command exits non-zero.

**Scenario 5: Auditor timeout**
GIVEN the scraper ran but the auditor exceeds 10 minutes.
WHEN the timeout is reached.
THEN the auditor container is killed, an `error` report is archived, and a `warning` notification is sent.

### Edge Cases

**Scenario 6: No new bids posted (weekend/holiday)**
GIVEN the scraper ran successfully but no new bids were posted.
WHEN the auditor evaluates scrape freshness (Scenario 5 in spec).
THEN freshness passes because the scrape_run itself is fresh (ran today), even if no new records were ingested.

**Scenario 7: Legacy email + workflow-notify coexistence**
GIVEN both the scraper's internal Gmail notification and workflow-notify are active.
WHEN the monitor command completes.
THEN both notification systems fire independently without interference.

## Resolved Ambiguities

1. **`run-and-audit.sh` deletion.** **Resolved:** Delete it. This is the last consumer. No rollback period needed.

2. **Cron log file.** **Resolved:** Share `~/logs/workflow-monitor.log` with ETL. All `workflow-orchestrate` output goes to one log.

## Implementation Constraints

- No code changes needed in `workflow-platform`. The `--exec` flag, container registry, timeout handling, and notification routing are already implemented and tested (84 tests passing).
- Changes are limited to:
  1. Update `specs/bid-scraper-access.md`: schedule from "6:00 AM ET via Dokploy cron" to "5:00 AM ET via workflow-orchestrate monitor --exec"
  2. Replace the bid-scraper crontab line with `workflow-orchestrate monitor --exec ...`
  3. Optionally delete `run-and-audit.sh`
- Manual validation: run the command once manually, confirm 10/10 audit pass, then apply crontab.
