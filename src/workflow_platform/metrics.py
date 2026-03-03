"""Push agent run metrics to Prometheus Pushgateway.

Metrics are best-effort: push failures log a warning but do not
affect the agent run outcome.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

log = structlog.get_logger("workflow_platform.metrics")

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "localhost:9091")


def push_metrics(service: str, role: str, report: dict[str, Any]) -> None:
    """Push agent run metrics to Pushgateway.

    Args:
        service: Service name (e.g., "bid-scraper").
        role: Agent role name (e.g., "auditor").
        report: The agent report dict with overall, duration_seconds,
            scenarios_pass, scenarios_fail fields.
    """
    registry = CollectorRegistry()

    result_gauge = Gauge(
        "agent_run_result",
        "Agent run result (1=pass, 0=fail)",
        ["service", "role"],
        registry=registry,
    )
    duration_gauge = Gauge(
        "agent_run_duration_seconds",
        "Agent run duration in seconds",
        ["service", "role"],
        registry=registry,
    )
    pass_gauge = Gauge(
        "agent_run_scenarios_pass",
        "Number of scenarios that passed",
        ["service", "role"],
        registry=registry,
    )
    fail_gauge = Gauge(
        "agent_run_scenarios_fail",
        "Number of scenarios that failed",
        ["service", "role"],
        registry=registry,
    )

    overall = report.get("overall", "error")
    normalized = 1 if overall in ("pass", "complete") else 0

    result_gauge.labels(service=service, role=role).set(normalized)
    duration_gauge.labels(service=service, role=role).set(report.get("duration_seconds", 0))
    pass_gauge.labels(service=service, role=role).set(report.get("scenarios_pass", 0))
    fail_gauge.labels(service=service, role=role).set(report.get("scenarios_fail", 0))

    push_to_gateway(
        PUSHGATEWAY_URL,
        job=f"workflow_agent_{service}_{role}",
        registry=registry,
    )
    log.info(
        "metrics.pushed",
        service=service,
        role=role,
        overall=overall,
        normalized=normalized,
    )
