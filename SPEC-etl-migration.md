# DefenderShield ETL Migration to Workflow Platform

## System Overview

Migrate the DefenderShield ETL service from its legacy orchestration layer (`run-and-audit.sh` shell script with raw Discord webhook) to the `workflow-orchestrate` CLI. The ETL container, Postgres database, Dokploy compose, and daily schedule remain unchanged. Only the cron wrapper and notification routing change. This also adds a general-purpose `--exec` flag to the `monitor` command so that future service migrations (e.g., bid-scraper) follow the same pattern.

## Behavioral Contract

### Service Execution via `--exec`

- When `workflow-orchestrate monitor` is called with an `--exec` flag, the system executes the specified command via `docker exec` against the service's container before running the audit.
- When `--exec` is provided, the system resolves the target container name from a service registry in config rather than requiring the caller to specify it.
- When the `--exec` command succeeds (exit 0), the system proceeds to the audit phase.
- When the `--exec` command fails (exit non-zero), the system logs the failure, sends a notification at `warning` severity, and still proceeds to the audit phase.
- When `--exec` is not provided, the system behaves exactly as it does today (audit-only).

### Container Availability

- When the target container is not running at exec time, the system detects this before attempting `docker exec`, sends a `critical` severity notification, and exits without running the audit (there is nothing to audit).

### Audit Phase

- When the audit completes, the system archives the report to `~/audit-reports/{service}/{mode}_{timestamp}/` and routes notifications via `workflow-notify`.
- When the auditor container itself fails (crash, timeout, Claude API error), the system treats this as an `error` distinct from a service failure or an audit `fail`, and routes a `warning` severity notification explaining that the auditor could not complete.
- When the auditor exceeds a 10-minute timeout, the system kills the auditor container and reports an `error` result with a timeout explanation.

### Notification Routing

- When any phase produces a result, notifications route through `workflow-notify.fanout()` with appropriate severity:
  - Audit `pass` -> `success` severity (Discord only)
  - Audit `fail` -> `warning` severity (Discord + Vault)
  - Audit `error` (auditor failure/timeout) -> `warning` severity (Discord + Vault)
  - Service exec failure -> `warning` severity (Discord + Vault)
  - Container not running -> `critical` severity (Discord + Vault + Email)
- When the ETL's own legacy email summary fires (from `daily_runner.py`), the system does not interfere. Both notification paths coexist.

### Schedule Continuity

- When the migration is complete, the host crontab entry at 6:15 AM ET calls `workflow-orchestrate monitor --service defendershield-etl --exec "python -m defendershield_etl.pipelines.daily_runner --catchup" --spec specs/defendershield-etl.md --access specs/defendershield-etl-access.md` instead of `run-and-audit.sh defendershield-etl`.
- When the bid-scraper crontab entry is updated in a future migration, the same `--exec` pattern applies. This spec does not modify bid-scraper's cron.

### Behavioral Spec Updates

- When the auditor evaluates Scenario 3 (sales data freshness), the threshold is 2 days, not 7.
- When the auditor evaluates Scenario 6 (forecast freshness), the threshold is 2 days, not 7.

## Explicit Non-Behaviors

- The system must not modify the ETL container, its image, its compose file, or its Dokploy configuration, because the ETL itself is not changing.
- The system must not modify the ETL's internal email notification logic (`_send_email()` in `daily_runner.py`), because legacy email coexists with `workflow-notify` intentionally.
- The system must not migrate the bid-scraper cron entry as part of this work, because that is a separate migration.
- The system must not add a `--container` CLI flag, because container names should be resolved from service configuration, not passed by the caller.
- The system must not retry a failed `--exec` command, because the ETL's `--catchup` mode is self-healing and will recover on the next daily run.
- The system must not delete `run-and-audit.sh`, because bid-scraper still depends on it until its own migration.

## Integration Boundaries

### Docker Engine (via `docker exec` / `docker run`)

- **Inbound:** Container name, command string.
- **Outbound:** Exit code, stdout/stderr.
- **Unavailable:** If the Docker daemon is unreachable, the entire `monitor` command fails. This is an infrastructure failure outside the system's scope -- no special handling beyond letting the error propagate.

### `workflow-notify` (via `fanout()`)

- **Inbound:** `service`, `severity`, `message`, optional `report` dict.
- **Outbound:** Notification delivery to Discord / Vault / Email per severity routing.
- **Unavailable:** If `workflow-notify` import fails or `fanout()` raises, log the error and continue. The audit result is still archived locally. This matches existing behavior in `orchestrate.py`.

### Auditor Container (`workflow-auditor:latest`)

- **Inbound:** Spec file, access doc, Claude auth (all mounted ro), env vars (`AUDITOR_MODE`, `AUDITOR_MODEL`, `AUDITOR_SERVICE`, `AUDITOR_MAX_TURNS`).
- **Outbound:** `report.json` + `report.md` written to `/audit/output/`.
- **Timeout:** 10 minutes. If exceeded, the container is killed (`docker kill`) and the system produces an error report.
- **Unavailable:** If the auditor image doesn't exist, `docker run` fails immediately. Route as `error`.

### Host Crontab

- **Change:** Replace `run-and-audit.sh defendershield-etl` line with `workflow-orchestrate monitor --exec ...` invocation.
- **Rollback:** Keep `run-and-audit.sh` on disk (do not delete) so it can be restored if the migration fails.

## Behavioral Scenarios

### Happy Path

**Scenario 1: Full successful run**
GIVEN the ETL container is running and healthy.
WHEN `workflow-orchestrate monitor --service defendershield-etl --exec "python -m defendershield_etl.pipelines.daily_runner --catchup" --spec specs/defendershield-etl.md --access specs/defendershield-etl-access.md` is invoked.
THEN the ETL command executes to completion, the auditor runs against the behavioral spec, the report is archived to `~/audit-reports/defendershield-etl/prod_*/`, and a notification is sent via `workflow-notify`.

**Scenario 2: Notification routing on pass**
GIVEN the ETL ran successfully and all 10 audit scenarios pass.
WHEN the monitor command completes.
THEN a `success` severity notification is sent to Discord only, and no email or Vault note is created.

**Scenario 3: Exec fails but audit still runs**
GIVEN the ETL container is running but the ETL command exits non-zero (e.g., SkuVault API timeout).
WHEN the monitor command processes the exec failure.
THEN a `warning` notification is sent about the exec failure, and the auditor still runs against the database to evaluate current data quality.

### Error Paths

**Scenario 4: Container not running**
GIVEN the ETL container (`ds-etl-nhdcjb-etl-scheduler-1`) is stopped or does not exist.
WHEN the monitor command attempts to execute with `--exec`.
THEN a `critical` notification is sent (Discord + Vault + Email), no `docker exec` is attempted, no audit runs, and the command exits non-zero.

**Scenario 5: Auditor timeout**
GIVEN the ETL ran successfully but the auditor container exceeds 10 minutes.
WHEN the timeout is reached.
THEN the auditor container is killed, an `error` report is generated with a timeout explanation, the report is archived, and a `warning` notification is sent.

### Edge Cases

**Scenario 6: Auditor image missing**
GIVEN `workflow-auditor:latest` does not exist on the host.
WHEN the monitor command attempts to run the auditor.
THEN an `error` report is generated, a `warning` notification is sent, and the command exits non-zero.

**Scenario 7: workflow-notify unavailable**
GIVEN `workflow-notify` cannot be imported or `fanout()` raises an exception.
WHEN the monitor command attempts to send a notification.
THEN the error is logged, the audit report is still archived locally, and the command exits with the audit result code (not the notification failure).

## Resolved Ambiguities

1. **Container name resolution.** **Resolved:** `service_containers` dict in `PlatformConfig` (`config.py`). Maps service name to Docker container name. Initial entries: `defendershield-etl` -> `ds-etl-nhdcjb-etl-scheduler-1`, `bid-scraper` -> `compose-bypass-solid-state-feed-6p6e3c-scraper-1`.

2. **Exec stdout/stderr handling.** **Resolved:** Saved to `exec_output.log` in the same archive directory as the audit report (`~/audit-reports/{service}/prod_{timestamp}/exec_output.log`).

3. **Cron stderr routing.** **Resolved:** Cron output goes to `~/logs/workflow-monitor.log` via `>> ~/logs/workflow-monitor.log 2>&1`.

## Remaining Notes

4. **Bid-scraper migration timing.** This spec migrates only `defendershield-etl`. The `run-and-audit.sh` script also handles `bid-scraper`. After this migration, the script still needs to exist for bid-scraper. When bid-scraper migrates later, the script can be retired. **No action needed now.**

## Implementation Constraints

- All changes go in the `workflow-platform` repo (`/home/docker/workflow-platform/`).
- The `--exec` flag is added to `cmd_monitor()` in `orchestrate.py` and the CLI parser in `main()`.
- An `--audit-timeout` flag (default 600 seconds / 10 minutes) is added to `cmd_monitor()` and passed through to `run_audit()` / the `docker run` call.
- The container name mapping must support at least `defendershield-etl` and `bid-scraper` (for future use).
- All 84 tests (73 existing + 11 new) must pass.
- After implementation: update host crontab, update `specs/defendershield-etl.md` (Scenarios 3 and 6: 7 days -> 2 days), update `specs/defendershield-etl-access.md` (schedule time: 6:00 -> 6:15 AM ET).
