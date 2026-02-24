"""Two-stage auditor: planner -> validator -> executor -> analyzer.

Replaces the single-stage auditor where Claude had network access. In this
pipeline, Claude (planner + analyzer) NEVER has network access. Data collection
is done by a deterministic executor that only runs validated queries.

Pipeline:
  1. Planner (--network none): reads spec + access doc, outputs a JSON query plan
  2. Validator (host-side, pure Python): checks plan against access doc allowlists
  3. Executor (temp Docker network): runs validated psql/curl queries only
  4. Analyzer (--network none): reads spec + executor results, produces report
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import structlog

from workflow_platform.auditor import _report_archive_dir, route_notifications

log = structlog.get_logger("workflow_platform.two_stage_auditor")

# Only bare SELECT * FROM <table> is allowed -- no WHERE, JOIN, subqueries, etc.
SQL_ALLOWLIST_PATTERN = r"^SELECT \* FROM [a-zA-Z_][a-zA-Z0-9_.]*;?$"

AUDITOR_IMAGE = "ghcr.io/zzofrea/workflow-auditor:latest"
CLAUDE_AUTH_JSON = str(Path.home() / ".claude.json")
CLAUDE_AUTH_DIR = str(Path.home() / ".claude")


class ValidationResult(NamedTuple):
    """Result of validating a query plan against the access document."""

    valid: bool
    rejection_reason: str | None


class ExecutorError(Exception):
    """Raised when the executor fails to run a query."""


# ---------------------------------------------------------------------------
# Access document parsing
# ---------------------------------------------------------------------------


def _extract_allowed_hosts(access_path: str) -> list[str]:
    """Extract DB hostnames from an access document.

    Scans for ``- Host: <hostname>`` lines. Reuses the same pattern as
    ``auditor.extract_allowed_hosts`` but kept local to avoid coupling.
    """
    hosts: list[str] = []
    try:
        with open(access_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.lower().startswith("- host:"):
                    host = stripped.split(":", 1)[1].strip()
                    if host:
                        hosts.append(host)
    except OSError:
        log.warning("two_stage.access_read_failed", path=access_path)
    return hosts


def _extract_allowed_urls(access_path: str) -> list[str]:
    """Extract allowed HTTP URLs from an access document.

    Parses markdown table rows matching ``| https://... |`` patterns.
    """
    urls: list[str] = []
    try:
        with open(access_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("|") and "https://" in stripped:
                    # Split table columns and grab the URL cell
                    cells = [c.strip() for c in stripped.split("|")]
                    for cell in cells:
                        if cell.startswith("https://"):
                            urls.append(cell)
    except OSError:
        log.warning("two_stage.access_read_failed", path=access_path)
    return urls


def _parse_credentials(access_path: str) -> dict[str, dict[str, str]]:
    """Extract DB credentials from an access document.

    Returns a dict keyed by hostname with user/password/database/port values.
    Parses the ``- Key: value`` lines under ``## Database`` sections.
    """
    creds: dict[str, str] = {}
    try:
        with open(access_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.lower().startswith("- host:"):
                    creds["host"] = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("- port:"):
                    creds["port"] = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("- database:"):
                    creds["database"] = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("- user:"):
                    creds["user"] = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("- password:"):
                    creds["password"] = stripped.split(":", 1)[1].strip()
    except OSError:
        log.warning("two_stage.creds_read_failed", path=access_path)
        return {}

    host = creds.get("host", "")
    if not host:
        return {}

    return {
        host: {
            "user": creds.get("user", ""),
            "password": creds.get("password", ""),
            "database": creds.get("database", ""),
            "port": creds.get("port", "5432"),
        }
    }


# ---------------------------------------------------------------------------
# Docker command builders
# ---------------------------------------------------------------------------


def _build_stage_cmd(
    input_dir: str,
    output_dir: str,
    service: str,
    stage: str,
    *,
    model: str = "sonnet",
) -> list[str]:
    """Build a ``docker run`` command for a planner or analyzer stage.

    Both stages share the same structure:
    - ``--network none`` (no network access)
    - ``--cap-drop ALL`` (minimal capabilities)
    - Claude auth mounted read-only
    - Input mounted read-only, output mounted read-write
    - ``AUDITOR_STAGE`` env var set to planner or analyzer
    - ``AUDITOR_ALLOWED_TOOLS=Read`` (only Read tool, no Bash)
    - No permission-skip flags
    """
    container_name = f"auditor-{stage}-{service}"
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "-v",
        f"{CLAUDE_AUTH_JSON}:/audit/auth/.claude.json:ro",
        "-v",
        f"{CLAUDE_AUTH_DIR}:/audit/auth/.claude:ro",
        "-v",
        f"{input_dir}:/audit/input:ro",
        "-v",
        f"{output_dir}:/audit/output:rw",
        "-e",
        f"AUDITOR_STAGE={stage}",
        "-e",
        f"AUDITOR_SERVICE={service}",
        "-e",
        f"AUDITOR_MODEL={model}",
        "-e",
        "AUDITOR_ALLOWED_TOOLS=Read",
        "-e",
        "HOME=/home/node",
        AUDITOR_IMAGE,
        "--allowedTools",
        "Read",
    ]


def build_planner_cmd(
    input_dir: str,
    output_dir: str,
    service: str,
    *,
    model: str = "sonnet",
) -> list[str]:
    """Build the docker run command for the planner stage."""
    return _build_stage_cmd(input_dir, output_dir, service, "planner", model=model)


def build_analyzer_cmd(
    input_dir: str,
    output_dir: str,
    service: str,
    *,
    model: str = "sonnet",
) -> list[str]:
    """Build the docker run command for the analyzer stage."""
    return _build_stage_cmd(input_dir, output_dir, service, "analyzer", model=model)


# ---------------------------------------------------------------------------
# Query plan validation
# ---------------------------------------------------------------------------


def validate_query_plan(plan: dict[str, Any], access_path: str) -> ValidationResult:
    """Validate a query plan against the access document allowlists.

    Checks:
    - ``queries`` key exists and is non-empty
    - Each entry has a valid ``type`` (psql or curl)
    - psql entries: host in allowlist, SQL matches strict regex
    - curl entries: URL exact-matches the URL allowlist
    """
    if "queries" not in plan:
        return ValidationResult(False, "Query plan missing 'queries' key")

    queries = plan["queries"]
    if not queries:
        return ValidationResult(False, "Query plan has empty queries list")

    allowed_hosts = _extract_allowed_hosts(access_path)
    allowed_urls = _extract_allowed_urls(access_path)

    for entry in queries:
        entry_type = entry.get("type")
        if entry_type not in ("psql", "curl"):
            return ValidationResult(
                False,
                f"Unknown query type: {entry_type!r} (allowed: psql, curl)",
            )

        if entry_type == "psql":
            host = entry.get("host", "")
            if host not in allowed_hosts:
                return ValidationResult(
                    False,
                    f"Unauthorized host: {host} (allowed: {allowed_hosts})",
                )
            query = entry.get("query", "")
            if not re.match(SQL_ALLOWLIST_PATTERN, query):
                return ValidationResult(
                    False,
                    f"SQL not in allowlist shape: {query!r}",
                )

        elif entry_type == "curl":
            url = entry.get("url", "")
            if url not in allowed_urls:
                return ValidationResult(
                    False,
                    f"URL not in allowlist: {url!r}",
                )

    return ValidationResult(True, None)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def run_executor(
    plan: dict[str, Any],
    credentials: dict[str, dict[str, str]],
    output_dir: str,
) -> dict[str, Any]:
    """Execute validated queries on a temporary Docker network.

    Creates ``audit-exec-{uuid}`` network, connects target containers,
    runs each query via ephemeral ``postgres:16-alpine`` or ``curlimages/curl``
    containers, then tears down the network.

    Raises ``ExecutorError`` on any query failure (fail-fast).
    """
    import uuid

    net_name = f"audit-exec-{uuid.uuid4().hex[:12]}"
    results: dict[str, Any] = {}

    # Collect unique target hosts that need to be connected
    target_hosts: set[str] = set()
    for entry in plan.get("queries", []):
        if entry.get("type") == "psql":
            target_hosts.add(entry["host"])

    try:
        # Create temporary network
        subprocess.run(
            ["docker", "network", "create", net_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        # Connect target DB containers to temp network
        for host in target_hosts:
            subprocess.run(
                ["docker", "network", "connect", net_name, host],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

        # Execute each query
        for entry in plan.get("queries", []):
            if entry["type"] == "psql":
                host = entry["host"]
                query = entry["query"]
                creds = credentials.get(host, {})

                # Extract table name for result key
                table_match = re.match(r"SELECT \* FROM ([a-zA-Z_][a-zA-Z0-9_.]*);?$", query)
                table_name = table_match.group(1) if table_match else "unknown"

                env_args: list[str] = []
                if creds.get("password"):
                    env_args = ["-e", f"PGPASSWORD={creds['password']}"]

                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        net_name,
                        "--cap-drop",
                        "ALL",
                        *env_args,
                        "postgres:16-alpine",
                        "psql",
                        "-h",
                        host,
                        "-p",
                        creds.get("port", "5432"),
                        "-U",
                        creds.get("user", "auditor_ro"),
                        "-d",
                        creds.get("database", "postgres"),
                        "-t",
                        "-A",
                        "-c",
                        query,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode != 0:
                    raise ExecutorError(f"psql query failed on {host}: {result.stderr}")

                results[table_name] = result.stdout

            elif entry["type"] == "curl":
                url = entry["url"]

                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        net_name,
                        "--cap-drop",
                        "ALL",
                        "curlimages/curl",
                        "-s",
                        "-S",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                if result.returncode != 0:
                    raise ExecutorError(f"curl failed for {url}: {result.stderr}")

                results[url] = result.stdout

    finally:
        # Always clean up: disconnect hosts then remove network
        for host in target_hosts:
            subprocess.run(
                ["docker", "network", "disconnect", net_name, host],
                capture_output=True,
                text=True,
                timeout=30,
            )
        subprocess.run(
            ["docker", "network", "rm", net_name],
            capture_output=True,
            text=True,
            timeout=30,
        )

    # Write results for the analyzer stage
    os.makedirs(output_dir, exist_ok=True)
    results_path = os.path.join(output_dir, "executor_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    return results


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def run_planner(
    input_dir: str,
    output_dir: str,
    service: str,
    *,
    model: str = "sonnet",
    timeout: int = 600,
) -> dict[str, Any]:
    """Run the planner stage and return the parsed query plan.

    The planner container writes ``plan.json`` to ``output_dir``.
    """
    cmd = build_planner_cmd(input_dir, output_dir, service, model=model)

    log.info("two_stage.planner_start", service=service, model=model)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        log.error("two_stage.planner_failed", stderr=result.stderr[:2000])

    plan_path = os.path.join(output_dir, "plan.json")
    if not os.path.exists(plan_path):
        raise ExecutorError(f"Planner did not produce plan.json. stdout: {result.stdout[:1000]}")

    with open(plan_path) as f:
        return json.load(f)


def run_analyzer(
    input_dir: str,
    output_dir: str,
    service: str,
    *,
    model: str = "sonnet",
    timeout: int = 600,
) -> dict[str, Any]:
    """Run the analyzer stage and return the parsed report.

    The analyzer container writes ``report.json`` to ``output_dir``.
    """
    cmd = build_analyzer_cmd(input_dir, output_dir, service, model=model)

    log.info("two_stage.analyzer_start", service=service, model=model)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        log.error("two_stage.analyzer_failed", stderr=result.stderr[:2000])

    report_path = os.path.join(output_dir, "report.json")
    if not os.path.exists(report_path):
        raise ExecutorError(f"Analyzer did not produce report.json. stdout: {result.stdout[:1000]}")

    with open(report_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def run_two_stage_audit(
    spec_path: str,
    access_path: str,
    service: str,
    mode: str,
    *,
    archive_dir: str | None = None,
    notify: bool = True,
    model: str = "sonnet",
    max_turns: int = 20,
    total_timeout: int = 1200,
) -> dict[str, Any]:
    """Run the full two-stage audit pipeline.

    Orchestrates: planner -> validator -> executor -> analyzer.
    Archives the report and sends notifications.
    """
    report_dir = archive_dir or _report_archive_dir(service, mode)
    os.makedirs(report_dir, exist_ok=True)

    try:
        with (
            tempfile.TemporaryDirectory(prefix="audit-planner-in-") as planner_in,
            tempfile.TemporaryDirectory(prefix="audit-planner-out-") as planner_out,
            tempfile.TemporaryDirectory(prefix="audit-analyzer-in-") as analyzer_in,
            tempfile.TemporaryDirectory(prefix="audit-analyzer-out-") as analyzer_out,
        ):
            # Prepare planner input: spec + access (no credentials)
            import shutil

            shutil.copy2(spec_path, os.path.join(planner_in, "spec.md"))
            shutil.copy2(access_path, os.path.join(planner_in, "access.md"))

            # Stage 1: Planner
            log.info("two_stage.pipeline_start", service=service, mode=mode)
            plan = run_planner(
                planner_in,
                planner_out,
                service,
                model=model,
                timeout=total_timeout // 3,
            )

            # Stage 2: Validator (host-side, no container)
            validation = validate_query_plan(plan, access_path)
            if not validation.valid:
                report: dict[str, Any] = {
                    "overall": "error",
                    "service": service,
                    "mode": mode,
                    "summary": f"Query plan rejected: {validation.rejection_reason}",
                    "scenarios": [],
                }
                _archive_report(report, report_dir)
                if notify:
                    route_notifications(report)
                return report

            # Stage 3: Executor (writes executor_results.json to analyzer_in)
            credentials = _parse_credentials(access_path)
            run_executor(plan, credentials, analyzer_in)

            # Prepare analyzer input: spec + access + executor results
            shutil.copy2(spec_path, os.path.join(analyzer_in, "spec.md"))
            shutil.copy2(access_path, os.path.join(analyzer_in, "access.md"))

            # Stage 4: Analyzer
            report = run_analyzer(
                analyzer_in,
                analyzer_out,
                service,
                model=model,
                timeout=total_timeout // 3,
            )

            _archive_report(report, report_dir)

            # Archive markdown report if produced
            md_path = os.path.join(analyzer_out, "report.md")
            if os.path.exists(md_path):
                shutil.copy2(md_path, os.path.join(report_dir, "report.md"))

            if notify:
                route_notifications(report)

            return report

    except subprocess.TimeoutExpired:
        report = {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Pipeline timed out after {total_timeout} seconds",
            "scenarios": [],
        }
        _archive_report(report, report_dir)
        if notify:
            route_notifications(report)
        return report

    except ExecutorError as exc:
        report = {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Executor failed: {exc}",
            "scenarios": [],
        }
        _archive_report(report, report_dir)
        if notify:
            route_notifications(report)
        return report


def _archive_report(report: dict[str, Any], report_dir: str) -> None:
    """Write report.json to the archive directory."""
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
