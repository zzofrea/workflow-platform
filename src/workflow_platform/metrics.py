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


def push_metrics(
    service: str,
    role: str,
    report: dict[str, Any],
    *,
    stage: str | None = None,
) -> None:
    """Push agent run metrics to Pushgateway.

    Args:
        service: Service name (e.g., "bid-scraper").
        role: Agent role name (e.g., "auditor").
        report: The agent report dict with overall, duration_seconds,
            scenarios_pass, scenarios_fail fields.
        stage: Optional stage name for per-stage DAG metrics granularity.
    """
    registry = CollectorRegistry()

    label_names = ["service", "role"]
    if stage is not None:
        label_names.append("stage")

    result_gauge = Gauge(
        "agent_run_result",
        "Agent run result (1=pass, 0=fail)",
        label_names,
        registry=registry,
    )
    duration_gauge = Gauge(
        "agent_run_duration_seconds",
        "Agent run duration in seconds",
        label_names,
        registry=registry,
    )
    pass_gauge = Gauge(
        "agent_run_scenarios_pass",
        "Number of scenarios that passed",
        label_names,
        registry=registry,
    )
    fail_gauge = Gauge(
        "agent_run_scenarios_fail",
        "Number of scenarios that failed",
        label_names,
        registry=registry,
    )

    overall = report.get("overall", "error")
    normalized = 1 if overall in ("pass", "complete") else 0

    labels: dict[str, str] = {"service": service, "role": role}
    if stage is not None:
        labels["stage"] = stage

    result_gauge.labels(**labels).set(normalized)
    duration_gauge.labels(**labels).set(report.get("duration_seconds", 0))
    pass_gauge.labels(**labels).set(report.get("scenarios_pass", 0))
    fail_gauge.labels(**labels).set(report.get("scenarios_fail", 0))

    job_suffix = f"_{stage}" if stage else ""
    push_to_gateway(
        PUSHGATEWAY_URL,
        job=f"workflow_agent_{service}_{role}{job_suffix}",
        registry=registry,
    )
    log.info(
        "metrics.pushed",
        service=service,
        role=role,
        stage=stage,
        overall=overall,
        normalized=normalized,
    )
