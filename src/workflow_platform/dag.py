"""YAML-driven DAG execution engine for workflow-orchestrate.

Loads a per-service DAG config, validates dependencies and conditions,
filters stages by day-of-week, resolves a topological execution order
with parallel tiers, and executes stages via docker-exec or workflow-agent.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, field_validator, model_validator

log = structlog.get_logger("workflow_platform.dag")

# Project root for resolving dags/ directory
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DAY_ABBREVS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class StageResult(StrEnum):
    """Outcome of a single stage execution."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"


class Stage(BaseModel):
    """A single stage in a DAG."""

    name: str
    type: str  # "docker-exec" or "agent"
    depends_on: list[str] = []
    condition: str | None = None
    when: list[str] | None = None
    timeout: int = 600

    # docker-exec fields
    container: str | None = None
    command: str | None = None

    # agent fields
    role: str | None = None
    max_turns: int = 50

    # day-of-month filtering (e.g. [1] for 1st of month)
    when_day_of_month: list[int] | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("docker-exec", "agent"):
            msg = f"Stage type must be 'docker-exec' or 'agent', got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("when")
    @classmethod
    def validate_when(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for day in v:
                if day.lower() not in DAY_ABBREVS:
                    msg = f"Invalid day abbreviation '{day}'. Must be one of: {DAY_ABBREVS}"
                    raise ValueError(msg)
            return [d.lower() for d in v]
        return v

    @field_validator("when_day_of_month")
    @classmethod
    def validate_when_day_of_month(cls, v: list[int] | None) -> list[int] | None:
        if v is not None:
            for day in v:
                if not isinstance(day, int) or day < 1 or day > 31:
                    msg = f"Invalid day of month '{day}'. Must be an integer 1-31."
                    raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_required_fields(self) -> Stage:
        if self.type == "docker-exec":
            if not self.container:
                msg = f"Stage '{self.name}': docker-exec requires 'container'"
                raise ValueError(msg)
            if not self.command:
                msg = f"Stage '{self.name}': docker-exec requires 'command'"
                raise ValueError(msg)
        elif self.type == "agent":
            if not self.role:
                msg = f"Stage '{self.name}': agent requires 'role'"
                raise ValueError(msg)
        return self


class DAGConfig(BaseModel):
    """Top-level DAG configuration loaded from YAML."""

    service: str
    schedule: str
    stages: list[Stage]

    @model_validator(mode="after")
    def validate_dag(self) -> DAGConfig:
        stage_names = {s.name for s in self.stages}

        # Check for duplicate names
        if len(stage_names) != len(self.stages):
            seen: set[str] = set()
            for s in self.stages:
                if s.name in seen:
                    msg = f"Duplicate stage name: '{s.name}'"
                    raise ValueError(msg)
                seen.add(s.name)

        # Check depends_on references exist
        for stage in self.stages:
            for dep in stage.depends_on:
                if dep not in stage_names:
                    msg = f"Stage '{stage.name}' depends on unknown stage '{dep}'"
                    raise ValueError(msg)

        # Check condition references exist
        for stage in self.stages:
            if stage.condition:
                ref_stage = stage.condition.rsplit(".", 1)[0]
                if ref_stage not in stage_names:
                    msg = f"Stage '{stage.name}' condition references unknown stage '{ref_stage}'"
                    raise ValueError(msg)
                if not stage.condition.endswith(".success"):
                    msg = (
                        f"Stage '{stage.name}' condition must end with '.success', "
                        f"got '{stage.condition}'"
                    )
                    raise ValueError(msg)

        # Check for cycles via DFS
        _detect_cycles(self.stages)

        return self


def _detect_cycles(stages: list[Stage]) -> None:
    """Detect circular dependencies via DFS. Raises ValueError if found."""
    adj: dict[str, list[str]] = {s.name: list(s.depends_on) for s in stages}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        in_stack.add(node)
        for neighbor in adj.get(node, []):
            if neighbor in in_stack:
                msg = f"Circular dependency detected: {node} -> {neighbor}"
                raise ValueError(msg)
            if neighbor not in visited:
                dfs(neighbor)
        in_stack.discard(node)

    for stage in stages:
        if stage.name not in visited:
            dfs(stage.name)


def load_dag(service: str) -> DAGConfig:
    """Load and validate a DAG config from dags/<service>.yaml."""
    dag_path = PROJECT_ROOT / "dags" / f"{service}.yaml"
    if not dag_path.exists():
        msg = f"DAG config not found: {dag_path}"
        raise FileNotFoundError(msg)

    with open(dag_path) as f:
        raw = yaml.safe_load(f)

    return DAGConfig(**raw)


def filter_stages(stages: list[Stage], utc_now: datetime) -> tuple[list[Stage], list[str]]:
    """Filter stages by day-of-week and/or day-of-month.

    Returns (active_stages, filtered_out_names).
    """
    today_idx = utc_now.weekday()  # 0=Monday
    today_day = utc_now.day  # 1-31
    active: list[Stage] = []
    filtered_out: list[str] = []

    for stage in stages:
        if stage.when is not None:
            if today_idx not in [DAY_INDEX[d] for d in stage.when]:
                filtered_out.append(stage.name)
                continue
        if stage.when_day_of_month is not None:
            if today_day not in stage.when_day_of_month:
                filtered_out.append(stage.name)
                continue
        active.append(stage)

    return active, filtered_out


def resolve_tiers(stages: list[Stage]) -> list[list[Stage]]:
    """Topological sort into parallel execution tiers.

    Stages with no unresolved dependencies go in the same tier (can run concurrently).
    """
    if not stages:
        return []

    stage_map = {s.name: s for s in stages}
    remaining = set(stage_map.keys())
    resolved: set[str] = set()
    tiers: list[list[Stage]] = []

    while remaining:
        # Find stages whose dependencies are all resolved (or not in remaining set)
        tier: list[Stage] = []
        for name in list(remaining):
            stage = stage_map[name]
            deps_in_remaining = [d for d in stage.depends_on if d in remaining]
            if not deps_in_remaining:
                tier.append(stage)

        if not tier:
            msg = f"Unable to resolve tiers, possible undetected cycle in: {remaining}"
            raise ValueError(msg)

        for stage in tier:
            remaining.discard(stage.name)
            resolved.add(stage.name)

        tiers.append(tier)

    return tiers


def _evaluate_condition(
    condition: str | None,
    results: dict[str, StageResult],
    filtered_out: set[str],
) -> bool:
    """Evaluate whether a condition is met.

    Returns True if the stage should execute, False if it should be skipped.
    """
    if condition is None:
        return True

    ref_stage = condition.rsplit(".", 1)[0]

    # Skipped stages (by when filter) did not succeed
    if ref_stage in filtered_out:
        return False

    result = results.get(ref_stage)
    if result is None:
        # Stage hasn't run yet -- shouldn't happen with proper tier ordering
        return False

    return result in (StageResult.PASS,)


def execute_stage(
    stage: Stage,
    results: dict[str, StageResult],
    service: str,
    filtered_out: set[str],
    *,
    run_agent_fn: Any = None,
    exec_service_fn: Any = None,
    check_container_fn: Any = None,
    push_metrics_fn: Any = None,
) -> StageResult:
    """Execute a single stage and return its result.

    Dependency functions are injected for testability.
    """
    start = time.monotonic()

    # Check condition gate
    if not _evaluate_condition(stage.condition, results, filtered_out):
        log.info("dag.stage_skipped", stage=stage.name, reason="condition not met")
        _push_stage_metrics(push_metrics_fn, service, stage, StageResult.SKIPPED, 0.0)
        return StageResult.SKIPPED

    log.info("dag.stage_start", stage=stage.name, type=stage.type)
    print(f"  [{stage.name}] Starting ({stage.type})...")

    agent_report: dict[str, Any] | None = None
    try:
        if stage.type == "docker-exec":
            result = _execute_docker_exec(
                stage,
                service,
                exec_service_fn=exec_service_fn,
                check_container_fn=check_container_fn,
            )
        elif stage.type == "agent":
            result, agent_report = _execute_agent(stage, service, run_agent_fn=run_agent_fn)
        else:
            result = StageResult.ERROR
    except Exception as exc:
        log.error("dag.stage_error", stage=stage.name, error=str(exc))
        result = StageResult.ERROR

    elapsed = time.monotonic() - start
    log.info(
        "dag.stage_complete",
        stage=stage.name,
        result=result.value,
        duration=f"{elapsed:.1f}s",
    )
    print(f"  [{stage.name}] {result.value.upper()} ({elapsed:.1f}s)")

    _push_stage_metrics(push_metrics_fn, service, stage, result, elapsed, report=agent_report)

    return result


def _execute_docker_exec(
    stage: Stage,
    service: str,
    *,
    exec_service_fn: Any = None,
    check_container_fn: Any = None,
) -> StageResult:
    """Run a docker-exec stage."""
    from workflow_platform.orchestrate import (
        _check_container_running,
        _exec_service,
    )

    check_fn = check_container_fn or _check_container_running
    exec_fn = exec_service_fn or _exec_service

    container = stage.container
    assert container is not None  # validated by model

    if not check_fn(container):
        log.error("dag.container_not_running", container=container)
        return StageResult.ERROR

    command: str = stage.command  # type: ignore[assignment]  # validated by model
    try:
        exit_code, stdout, stderr = exec_fn(container, command, service=service)
    except Exception as exc:
        log.error("dag.exec_error", stage=stage.name, error=str(exc))
        return StageResult.ERROR

    # Archive exec output
    output_path = archive_exec_output(service, stage.name, stdout, stderr, exit_code)

    # Copy report artifacts from container if they exist
    if exit_code == 0 and output_path is not None:
        _copy_report_artifacts(container, output_path.parent)

    return StageResult.PASS if exit_code == 0 else StageResult.FAIL


def _execute_agent(
    stage: Stage,
    service: str,
    *,
    run_agent_fn: Any = None,
) -> tuple[StageResult, dict[str, Any]]:
    """Run an agent stage, returning (result, report) so metrics get real scenario counts."""
    from workflow_platform.orchestrate import _run_workflow_agent

    agent_fn = run_agent_fn or _run_workflow_agent

    assert stage.role is not None  # validated by model

    report, _run_id = agent_fn(
        service,
        stage.role,
        max_turns=stage.max_turns,
        timeout=stage.timeout,
    )

    overall = report.get("overall", "error")
    if overall in ("pass", "complete"):
        return StageResult.PASS, report
    if overall == "error":
        return StageResult.ERROR, report
    return StageResult.FAIL, report


def archive_exec_output(
    service: str,
    stage_name: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> Path | None:
    """Save docker-exec output to ~/agent-output/<service>/exec_<timestamp>/output.log."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
    output_dir = Path.home() / "agent-output" / service / f"exec_{stage_name}_{timestamp}"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f"=== STAGE: {stage_name} ===\n"
            f"=== EXIT CODE: {exit_code} ===\n\n"
            f"=== STDOUT ===\n{stdout}\n=== STDERR ===\n{stderr}"
        )
        output_path = output_dir / "output.log"
        output_path.write_text(content)
        log.info("dag.exec_output_archived", path=str(output_path))
        return output_path
    except OSError as exc:
        log.warning("dag.archive_failed", error=str(exc))
        return None


def _copy_report_artifacts(container: str, archive_dir: Path) -> None:
    """Copy report artifacts from container /tmp/ to archive directory.

    Best-effort: logs warnings on failure, does not affect stage result.
    """
    import subprocess

    artifacts = ["report_metrics.json", "report_email.html"]
    for filename in artifacts:
        try:
            result = subprocess.run(
                ["docker", "cp", f"{container}:/tmp/{filename}", str(archive_dir / filename)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                log.info("dag.artifact_copied", file=filename, dest=str(archive_dir))
            else:
                log.debug("dag.artifact_not_found", file=filename)
        except Exception as exc:
            log.warning("dag.artifact_copy_failed", file=filename, error=str(exc))


def _push_stage_metrics(
    push_metrics_fn: Any,
    service: str,
    stage: Stage,
    result: StageResult,
    duration: float,
    *,
    report: dict[str, Any] | None = None,
) -> None:
    """Push per-stage metrics to Pushgateway (best-effort).

    For agent stages, pass the real report so scenario counts are accurate.
    For docker-exec stages, report is None and a synthetic dict is used.
    """
    try:
        if push_metrics_fn is not None:
            push_metrics_fn(service, stage, result, duration)
            return

        from workflow_platform.metrics import push_metrics

        actual_report = report or {
            "overall": result.value,
            "role": stage.role or stage.name,
            "duration_seconds": duration,
            "scenarios_pass": 0,
            "scenarios_fail": 0,
        }
        push_metrics(service, stage.role or stage.name, actual_report, stage=stage.name)
    except Exception as exc:
        log.warning("dag.metrics_push_failed", stage=stage.name, error=str(exc))


def execute_dag(
    dag: DAGConfig,
    *,
    utc_now: datetime | None = None,
    run_agent_fn: Any = None,
    exec_service_fn: Any = None,
    check_container_fn: Any = None,
    push_metrics_fn: Any = None,
) -> dict[str, StageResult]:
    """Execute a full DAG: filter, resolve tiers, run stages.

    Returns a dict mapping stage name -> result.
    """
    now = utc_now or datetime.now(UTC)
    dag_start = time.monotonic()

    active_stages, filtered_names = filter_stages(dag.stages, now)
    filtered_set = set(filtered_names)

    log.info(
        "dag.start",
        service=dag.service,
        total_stages=len(dag.stages),
        active_stages=len(active_stages),
        filtered_out=filtered_names,
    )
    print(
        f"=== DAG: {dag.service} ({len(active_stages)} active, {len(filtered_names)} filtered) ==="
    )

    if not active_stages:
        log.info("dag.no_stages", service=dag.service)
        print("No stages to run.")
        return {}

    tiers = resolve_tiers(active_stages)
    results: dict[str, StageResult] = {}

    for tier_idx, tier in enumerate(tiers):
        log.info(
            "dag.tier_start",
            tier=tier_idx,
            stages=[s.name for s in tier],
        )

        if len(tier) == 1:
            # Single stage -- run directly, no thread overhead
            stage = tier[0]
            results[stage.name] = execute_stage(
                stage,
                results,
                dag.service,
                filtered_set,
                run_agent_fn=run_agent_fn,
                exec_service_fn=exec_service_fn,
                check_container_fn=check_container_fn,
                push_metrics_fn=push_metrics_fn,
            )
        else:
            # Multiple stages -- run concurrently
            with ThreadPoolExecutor(max_workers=len(tier)) as executor:
                futures = {
                    executor.submit(
                        execute_stage,
                        stage,
                        results,
                        dag.service,
                        filtered_set,
                        run_agent_fn=run_agent_fn,
                        exec_service_fn=exec_service_fn,
                        check_container_fn=check_container_fn,
                        push_metrics_fn=push_metrics_fn,
                    ): stage
                    for stage in tier
                }
                for future in as_completed(futures):
                    stage = futures[future]
                    try:
                        results[stage.name] = future.result()
                    except Exception as exc:
                        log.error("dag.stage_exception", stage=stage.name, error=str(exc))
                        results[stage.name] = StageResult.ERROR

    elapsed = time.monotonic() - dag_start
    any_failed = any(r in (StageResult.FAIL, StageResult.ERROR) for r in results.values())
    status = "FAIL" if any_failed else "PASS"

    log.info(
        "dag.complete",
        service=dag.service,
        status=status,
        duration=f"{elapsed:.1f}s",
        results={k: v.value for k, v in results.items()},
    )
    print(f"=== DAG {status} ({elapsed:.1f}s) ===")

    return results
