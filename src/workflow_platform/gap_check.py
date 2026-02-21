"""Gap detection for pipeline run logs.

Queries a service's run-log table for the most recent successful run.
If the gap exceeds a threshold, fires a warning via workflow-notify.

Usage:
    python -m workflow_platform.gap_check --service bid-scraper \
        --db-url postgresql://... --threshold 36
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

import structlog
from workflow_notify import NotifyConfig, fanout

log = structlog.get_logger("workflow_platform.gap_check")

# Service-specific queries for finding the last successful run.
# Each returns a single row with columns: finished_at (timestamptz or None).
SERVICE_QUERIES: dict[str, str] = {
    "bid-scraper": """
        SELECT finished_at
        FROM scrape_runs
        WHERE status = 'success'
        ORDER BY finished_at DESC
        LIMIT 1
    """,
}

DEFAULT_THRESHOLD_HOURS = 36.0


def check_gap(
    *,
    service: str,
    db_url: str,
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS,
    notify_config: NotifyConfig | None = None,
) -> dict:
    """Check for gaps in a service's run history.

    Args:
        service: Service name (must have an entry in SERVICE_QUERIES).
        db_url: PostgreSQL connection URL.
        threshold_hours: Fire warning if last success is older than this.
        notify_config: Notification config. If None, loads from env vars.

    Returns:
        Dict with keys: service, status ("ok"|"stale"|"no_runs"|"db_error"),
        last_success (ISO string or None), gap_hours (float or None).
    """
    if notify_config is None:
        notify_config = NotifyConfig()

    query = SERVICE_QUERIES.get(service)
    if query is None:
        log.error("gap_check.unknown_service", service=service, known=list(SERVICE_QUERIES))
        return {"service": service, "status": "unknown_service"}

    try:
        last_success = _query_last_success(db_url, query)
    except Exception as exc:
        log.error(
            "gap_check.db_error",
            service=service,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        fanout(
            config=notify_config,
            service=service,
            severity="critical",
            message=f"Gap check failed: database unreachable ({type(exc).__name__})",
            observation=f"Could not connect to {service} database to check run history.",
            evidence=str(exc),
            suggested_action="Verify database container is running and credentials are correct.",
        )
        return {"service": service, "status": "db_error", "error": str(exc)}

    if last_success is None:
        log.warning("gap_check.no_runs", service=service)
        fanout(
            config=notify_config,
            service=service,
            severity="warning",
            message="No successful runs found in run history",
            observation=f"The {service} run-log table has no rows with status 'success'.",
            suggested_action="Verify the service has been run at least once.",
        )
        return {"service": service, "status": "no_runs", "last_success": None, "gap_hours": None}

    now = datetime.now(UTC)
    gap = now - last_success
    gap_hours = round(gap.total_seconds() / 3600, 1)

    if gap_hours > threshold_hours:
        log.warning(
            "gap_check.stale",
            service=service,
            gap_hours=gap_hours,
            threshold_hours=threshold_hours,
            last_success=last_success.isoformat(),
        )
        fanout(
            config=notify_config,
            service=service,
            severity="warning",
            message=f"Last successful run was {gap_hours}h ago (threshold: {threshold_hours}h)",
            observation=(
                f"The {service} pipeline has not completed successfully in {gap_hours} hours."
            ),
            evidence=f"Last successful run: {last_success.isoformat()}",
            expected_behavior=f"A successful run within the last {threshold_hours} hours.",
            suggested_action="Check service logs and container status.",
        )
        return {
            "service": service,
            "status": "stale",
            "last_success": last_success.isoformat(),
            "gap_hours": gap_hours,
        }

    log.info(
        "gap_check.ok",
        service=service,
        gap_hours=gap_hours,
        last_success=last_success.isoformat(),
    )
    return {
        "service": service,
        "status": "ok",
        "last_success": last_success.isoformat(),
        "gap_hours": gap_hours,
    }


def _query_last_success(db_url: str, query: str) -> datetime | None:
    """Execute a query and return the finished_at timestamp, or None."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL(query))  # type: ignore[arg-type]  # query from SERVICE_QUERIES constants
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None
            return row[0]


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Check for pipeline run gaps")
    parser.add_argument("--service", required=True, help="Service name (e.g., bid-scraper)")
    parser.add_argument("--db-url", required=True, help="PostgreSQL connection URL")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_HOURS,
        help=f"Warning threshold in hours (default: {DEFAULT_THRESHOLD_HOURS})",
    )
    args = parser.parse_args()

    result = check_gap(
        service=args.service,
        db_url=args.db_url,
        threshold_hours=args.threshold,
    )

    if result["status"] in ("stale", "no_runs", "db_error"):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
