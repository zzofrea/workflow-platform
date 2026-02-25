"""Host-side auditor wrapper: build input, run container, collect report, notify.

Usage:
    workflow-audit run --service bid-scraper --spec path/to/spec.md --access path/to/access.md
    workflow-audit run --service bid-scraper --spec spec.md --access access.md --mode prod
    workflow-audit run --service bid-scraper --spec spec.md --access access.md --model opus
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

import structlog

try:
    from workflow_notify import NotifyConfig, fanout
except ImportError:
    NotifyConfig = None  # type: ignore[assignment,misc]
    fanout = None  # type: ignore[assignment]

log = structlog.get_logger("workflow_platform.auditor")

AUDITOR_IMAGE = "ghcr.io/zzofrea/workflow-auditor:latest"
CONTAINER_NAME_PREFIX = "auditor"
CLAUDE_AUTH_JSON = str(Path.home() / ".claude.json")
CLAUDE_AUTH_DIR = str(Path.home() / ".claude")


def build_image(dockerfile_dir: str) -> bool:
    """Build the auditor Docker image locally (dev convenience).

    For production, the image is built by GitHub Actions and pushed to GHCR.
    This command is useful during local development to iterate on the Dockerfile.
    """
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            os.path.join(dockerfile_dir, "Dockerfile.auditor"),
            "-t",
            AUDITOR_IMAGE,
            dockerfile_dir,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        log.error("auditor.build_failed", stderr=result.stderr[:2000])
        return False
    log.info("auditor.image_built", image=AUDITOR_IMAGE)
    return True


def pull_image() -> bool:
    """Pull the auditor image from GHCR."""
    result = subprocess.run(
        ["docker", "pull", AUDITOR_IMAGE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        log.error("auditor.pull_failed", stderr=result.stderr[:2000])
        return False
    log.info("auditor.image_pulled", image=AUDITOR_IMAGE)
    return True


def _image_exists_locally() -> bool:
    """Check if the auditor image is already available locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", AUDITOR_IMAGE],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def extract_allowed_hosts(access_path: str) -> list[str]:
    """Extract DB/service hostnames from an access document.

    Scans for '- Host: <hostname>' lines in the access doc. These are the only
    hosts the auditor container will be allowed to reach via psql/curl.
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
        log.warning("auditor.access_read_failed", path=access_path)
    return hosts


def prepare_input(input_dir: str, spec_path: str, access_path: str) -> None:
    """Copy spec and access docs into the container input directory."""
    shutil.copy2(spec_path, os.path.join(input_dir, "spec.md"))
    shutil.copy2(access_path, os.path.join(input_dir, "access.md"))


def build_docker_cmd(
    input_dir: str,
    output_dir: str,
    *,
    service: str,
    mode: str = "build",
    model: str = "sonnet",
    max_turns: int = 20,
    network: str = "dokploy-network",
    allowed_hosts: list[str] | None = None,
) -> list[str]:
    """Construct the docker run command for the auditor container."""
    container_name = f"{CONTAINER_NAME_PREFIX}-{service}-{mode}"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        network,
        # Mount Claude auth to staging dir (read-only, copied to home at startup)
        "-v",
        f"{CLAUDE_AUTH_JSON}:/audit/auth/.claude.json:ro",
        "-v",
        f"{CLAUDE_AUTH_DIR}:/audit/auth/.claude:ro",
        # Mount input (read-only)
        "-v",
        f"{input_dir}:/audit/input:ro",
        # Mount output (read-write)
        "-v",
        f"{output_dir}:/audit/output:rw",
        # Environment variables
        "-e",
        f"AUDITOR_MODE={mode}",
        "-e",
        f"AUDITOR_MODEL={model}",
        "-e",
        f"AUDITOR_SERVICE={service}",
        "-e",
        f"AUDITOR_MAX_TURNS={max_turns}",
        # Scoped host access -- only declared DB/service hosts
        "-e",
        f"AUDITOR_ALLOWED_HOSTS={','.join(allowed_hosts or [])}",
        # Run as node user (Claude CLI default)
        "-e",
        "HOME=/home/node",
        AUDITOR_IMAGE,
    ]
    return cmd


def run_audit(
    spec_path: str,
    access_path: str,
    *,
    service: str,
    mode: str = "build",
    model: str = "sonnet",
    max_turns: int = 20,
    network: str = "dokploy-network",
    notify: bool = True,
    audit_timeout: int = 600,
    archive_dir: str | None = None,
) -> dict[str, Any]:
    """Run a full audit cycle: prepare, execute container, collect report.

    Args:
        audit_timeout: Max seconds for the auditor container before it is killed.
            Defaults to 600 (10 minutes).
        archive_dir: Pre-created directory for archiving reports. If None, a new
            timestamped directory is created under ~/audit-reports/{service}/.

    Returns the parsed report dict.
    """
    # Ensure the auditor image is available (pull from GHCR if pruned)
    if not _image_exists_locally():
        log.info("auditor.image_missing_locally", image=AUDITOR_IMAGE)
        if not pull_image():
            log.error("auditor.image_unavailable", image=AUDITOR_IMAGE)
            print(f"Error: Could not pull auditor image: {AUDITOR_IMAGE}", file=sys.stderr)
            sys.exit(1)

    # Validate inputs exist
    if not os.path.isfile(spec_path):
        log.error("auditor.spec_not_found", path=spec_path)
        print(f"Error: Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(access_path):
        log.error("auditor.access_not_found", path=access_path)
        print(f"Error: Access file not found: {access_path}", file=sys.stderr)
        sys.exit(1)

    # Create temp directories for input/output
    with (
        tempfile.TemporaryDirectory(prefix="auditor-input-") as input_dir,
        tempfile.TemporaryDirectory(prefix="auditor-output-") as output_dir,
    ):
        # Prepare input
        prepare_input(input_dir, spec_path, access_path)

        # Extract allowed hosts from access doc for tool scoping
        allowed_hosts = extract_allowed_hosts(access_path)
        log.info(
            "auditor.input_prepared",
            spec=spec_path,
            access=access_path,
            input_dir=input_dir,
            allowed_hosts=allowed_hosts,
        )

        # Build docker command
        cmd = build_docker_cmd(
            input_dir,
            output_dir,
            service=service,
            mode=mode,
            model=model,
            max_turns=max_turns,
            network=network,
            allowed_hosts=allowed_hosts,
        )

        container_name = f"{CONTAINER_NAME_PREFIX}-{service}-{mode}"
        log.info("auditor.container_starting", service=service, mode=mode, model=model)
        print(f"Starting auditor: service={service} mode={mode} model={model}")

        # Run the container with timeout
        timed_out = False
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=audit_timeout,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning(
                "auditor.timeout",
                service=service,
                timeout_seconds=audit_timeout,
            )
            print(f"Auditor timed out after {audit_timeout}s -- killing container")
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                timeout=30,
            )
            # Also clean up the container (--rm may not fire after kill)
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30,
            )

        if not timed_out:
            print(result.stdout)
            if result.stderr:
                print(result.stderr, file=sys.stderr)

        # Collect report
        report_json_path = os.path.join(output_dir, "report.json")
        report_md_path = os.path.join(output_dir, "report.md")

        report: dict[str, Any] = {}
        if timed_out:
            report = {
                "mode": mode,
                "service": service,
                "overall": "error",
                "summary": f"Auditor timed out after {audit_timeout} seconds",
                "scenarios": [],
            }
            log.error("auditor.timeout_report", service=service)
        elif os.path.exists(report_json_path):
            with open(report_json_path) as f:
                report = json.load(f)
            log.info(
                "auditor.report_collected",
                overall=report.get("overall"),
                scenarios=report.get("scenarios_total"),
            )
        else:
            report = {
                "mode": mode,
                "service": service,
                "overall": "error",
                "summary": "No report produced by auditor container",
                "scenarios": [],
                "raw_output": result.stdout[:5000],
            }
            log.error("auditor.no_report", stdout=result.stdout[:500])

        # Copy reports to a persistent location
        report_dir = archive_dir or _report_archive_dir(service, mode)
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        if os.path.exists(report_md_path):
            shutil.copy2(report_md_path, os.path.join(report_dir, "report.md"))

        print(f"Report archived to {report_dir}/")

        # Route notifications
        if notify:
            route_notifications(report)

        return report


# ---------------------------------------------------------------------------
# Auditor v2: temp-network bridge with direct DB access
# ---------------------------------------------------------------------------

ALLOWED_TOOLS_V2 = "Read,Bash(psql*),Bash(python3*),Bash(date*)"


def _parse_credentials(access_path: str) -> dict[str, str]:
    """Extract DB credentials from an access document.

    Returns a flat dict with host/port/database/user/password keys.
    If password contains "none" or "trust" (case-insensitive), it is
    treated as empty (trust authentication).
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
                    raw_pw = stripped.split(":", 1)[1].strip()
                    # Trust auth: password field says "none" or "trust"
                    if re.search(r"\b(none|trust)\b", raw_pw, re.IGNORECASE):
                        creds["password"] = ""
                    else:
                        creds["password"] = raw_pw
    except OSError:
        log.warning("auditor.creds_read_failed", path=access_path)

    return creds


def _resolve_container_names(hostnames: set[str]) -> dict[str, str]:
    """Resolve Docker DNS hostnames to actual container names.

    On dokploy-network, containers are addressable by hostname or alias
    (e.g., ``gov-bid-postgres``), but ``docker network connect``
    requires the real container name (e.g.,
    ``compose-bypass-solid-state-feed-6p6e3c-postgres-1``).

    Returns a dict mapping hostname -> container name. Falls back to the
    hostname itself if resolution fails (which will error at connect time).
    """
    if not hostnames:
        return {}

    mapping: dict[str, str] = {}

    # Get all containers on dokploy-network with their names
    result = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "dokploy-network",
            "--format",
            "{{range .Containers}}{{.Name}} {{end}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        log.warning("auditor.network_inspect_failed", stderr=result.stderr[:500])
        return {h: h for h in hostnames}

    # For each container on the network, check if its hostname or aliases
    # match any of the target hostnames
    containers = result.stdout.strip().split()
    for container in containers:
        if not container:
            continue

        # Check container's configured hostname
        hostname_result = subprocess.run(
            ["docker", "inspect", container, "--format", "{{.Config.Hostname}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if hostname_result.returncode == 0:
            ctr_hostname = hostname_result.stdout.strip()
            if ctr_hostname in hostnames:
                mapping[ctr_hostname] = container

        # Check network aliases on dokploy-network
        alias_result = subprocess.run(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .NetworkSettings.Networks "dokploy-network" "Aliases"}}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if alias_result.returncode == 0:
            aliases_str = alias_result.stdout.strip().strip("[]")
            for alias in aliases_str.split():
                if alias in hostnames:
                    mapping[alias] = container

    # Fall back to hostname for anything unresolved
    for h in hostnames:
        if h not in mapping:
            mapping[h] = h

    return mapping


def _check_db_running(container_name: str) -> bool:
    """Check if a Docker container is running via docker inspect."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _build_v2_docker_cmd(
    input_dir: str,
    output_dir: str,
    *,
    service: str,
    mode: str = "prod",
    model: str = "sonnet",
    max_turns: int = 50,
    network: str,
    creds: dict[str, str],
) -> list[str]:
    """Construct the docker run command for the v2 auditor container.

    Uses a temporary network (not dokploy-network), passes PG connection
    details via standard libpq env vars, and scopes tools to psql/python3/date.
    """
    container_name = f"{CONTAINER_NAME_PREFIX}-{service}-{mode}"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        network,
        "--cap-drop",
        "ALL",
        # Mount Claude auth to staging dir (read-only, copied to home at startup)
        "-v",
        f"{CLAUDE_AUTH_JSON}:/audit/auth/.claude.json:ro",
        "-v",
        f"{CLAUDE_AUTH_DIR}:/audit/auth/.claude:ro",
        # Mount input (read-only)
        "-v",
        f"{input_dir}:/audit/input:ro",
        # Mount output (read-write)
        "-v",
        f"{output_dir}:/audit/output:rw",
        # Environment variables
        "-e",
        f"AUDITOR_STAGE=v2",
        "-e",
        f"AUDITOR_MODE={mode}",
        "-e",
        f"AUDITOR_MODEL={model}",
        "-e",
        f"AUDITOR_SERVICE={service}",
        "-e",
        f"AUDITOR_MAX_TURNS={max_turns}",
        # Standard libpq env vars for psql
        "-e",
        f"PGHOST={creds.get('host', '')}",
        "-e",
        f"PGPORT={creds.get('port', '5432')}",
        "-e",
        f"PGUSER={creds.get('user', '')}",
        "-e",
        f"PGDATABASE={creds.get('database', '')}",
    ]

    # Only set PGPASSWORD if there's an actual password (trust auth omits it)
    password = creds.get("password", "")
    if password:
        cmd.extend(["-e", f"PGPASSWORD={password}"])

    cmd.extend(
        [
            "-e",
            "HOME=/home/node",
            AUDITOR_IMAGE,
        ]
    )

    return cmd


def run_audit_v2(
    spec_path: str,
    access_path: str,
    service: str,
    mode: str,
    *,
    archive_dir: str | None = None,
    notify: bool = True,
    model: str = "sonnet",
    max_turns: int = 50,
    total_timeout: int = 300,
) -> dict[str, Any]:
    """Run a v2 audit: temp network bridge + single Claude auditor with direct DB access.

    Creates a temporary Docker network, connects the target DB container to it,
    launches the auditor container with PG env vars, collects the report, and
    cleans up the network.

    Args:
        total_timeout: Max seconds for the auditor container before it is killed.
            Defaults to 300 (5 minutes).

    Returns the parsed report dict.
    """
    # Ensure the auditor image is available
    if not _image_exists_locally():
        log.info("auditor.image_missing_locally", image=AUDITOR_IMAGE)
        if not pull_image():
            log.error("auditor.image_unavailable", image=AUDITOR_IMAGE)
            return {
                "overall": "error",
                "service": service,
                "mode": mode,
                "summary": f"Could not pull auditor image: {AUDITOR_IMAGE}",
                "scenarios": [],
            }

    # Validate inputs exist
    if not os.path.isfile(spec_path):
        log.error("auditor.spec_not_found", path=spec_path)
        return {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Spec file not found: {spec_path}",
            "scenarios": [],
        }
    if not os.path.isfile(access_path):
        log.error("auditor.access_not_found", path=access_path)
        return {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Access file not found: {access_path}",
            "scenarios": [],
        }

    # Parse credentials from access doc
    creds = _parse_credentials(access_path)
    hostname = creds.get("host", "")
    if not hostname:
        return {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": "No database hostname found in access document",
            "scenarios": [],
        }

    # Resolve hostname to actual Docker container name
    host_to_container = _resolve_container_names({hostname})
    container_name = host_to_container.get(hostname, hostname)

    # Check DB container is running (fail early)
    if not _check_db_running(container_name):
        report: dict[str, Any] = {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Database container '{container_name}' is not running",
            "scenarios": [],
        }
        report_dir = archive_dir or _report_archive_dir(service, mode)
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        if notify:
            route_notifications(report)
        return report

    # Create temp network
    net_name = f"audit-{service}-{uuid.uuid4().hex[:12]}"
    auditor_container = f"{CONTAINER_NAME_PREFIX}-{service}-{mode}"

    try:
        # Create temporary network
        subprocess.run(
            ["docker", "network", "create", net_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        log.info("auditor.network_created", network=net_name)

        # Connect DB to temp network with hostname alias
        subprocess.run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                hostname,
                net_name,
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        log.info(
            "auditor.db_connected",
            network=net_name,
            container=container_name,
            alias=hostname,
        )

        # Create temp directories for input/output
        with (
            tempfile.TemporaryDirectory(prefix="auditor-input-") as input_dir,
            tempfile.TemporaryDirectory(prefix="auditor-output-") as output_dir,
        ):
            # Prepare input
            prepare_input(input_dir, spec_path, access_path)

            # Build docker command
            cmd = _build_v2_docker_cmd(
                input_dir,
                output_dir,
                service=service,
                mode=mode,
                model=model,
                max_turns=max_turns,
                network=net_name,
                creds=creds,
            )

            log.info(
                "auditor.container_starting",
                service=service,
                mode=mode,
                model=model,
                network=net_name,
            )
            print(f"Starting auditor v2: service={service} mode={mode} model={model}")

            # Run the container with timeout
            timed_out = False
            result = subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr="")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=total_timeout,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning(
                    "auditor.timeout",
                    service=service,
                    timeout_seconds=total_timeout,
                )
                print(f"Auditor timed out after {total_timeout}s -- killing container")
                subprocess.run(
                    ["docker", "kill", auditor_container],
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    ["docker", "rm", "-f", auditor_container],
                    capture_output=True,
                    timeout=30,
                )

            if not timed_out:
                if result.stdout:
                    print(result.stdout)
                if result.stderr:
                    print(result.stderr, file=sys.stderr)

            # Collect report
            report_json_path = os.path.join(output_dir, "report.json")
            report_md_path = os.path.join(output_dir, "report.md")

            report = {}
            if timed_out:
                report = {
                    "mode": mode,
                    "service": service,
                    "overall": "error",
                    "summary": f"Auditor timed out after {total_timeout} seconds",
                    "scenarios": [],
                }
            elif os.path.exists(report_json_path):
                with open(report_json_path) as f:
                    report = json.load(f)
                log.info(
                    "auditor.report_collected",
                    overall=report.get("overall"),
                    scenarios=report.get("scenarios_total"),
                )
            else:
                raw_stdout = result.stdout[:5000] if not timed_out else ""
                report = {
                    "mode": mode,
                    "service": service,
                    "overall": "error",
                    "summary": "No report produced by auditor container",
                    "scenarios": [],
                    "raw_output": raw_stdout,
                }
                log.error("auditor.no_report", stdout=raw_stdout[:500])

            # Archive report
            report_dir = archive_dir or _report_archive_dir(service, mode)
            os.makedirs(report_dir, exist_ok=True)
            with open(os.path.join(report_dir, "report.json"), "w") as f:
                json.dump(report, f, indent=2)
            if os.path.exists(report_md_path):
                shutil.copy2(report_md_path, os.path.join(report_dir, "report.md"))

            print(f"Report archived to {report_dir}/")

            # Route notifications
            if notify:
                route_notifications(report)

            return report

    except subprocess.CalledProcessError as exc:
        log.error(
            "auditor.network_setup_failed",
            network=net_name,
            error=str(exc),
        )
        report = {
            "overall": "error",
            "service": service,
            "mode": mode,
            "summary": f"Network setup failed: {exc}",
            "scenarios": [],
        }
        report_dir = archive_dir or _report_archive_dir(service, mode)
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        if notify:
            route_notifications(report)
        return report

    finally:
        # Always clean up: disconnect DB from temp network, remove temp network
        subprocess.run(
            ["docker", "network", "disconnect", net_name, container_name],
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
        log.info("auditor.network_cleaned", network=net_name)


def _report_archive_dir(service: str, mode: str) -> str:
    """Get the archive directory for audit reports."""
    from datetime import datetime

    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(
        str(Path.home()),
        "audit-reports",
        service,
        f"{mode}_{date_str}",
    )


def route_notifications(report: dict[str, Any]) -> None:
    """Send notifications based on audit findings."""
    if NotifyConfig is None or fanout is None:
        log.warning("auditor.notify_unavailable", reason="workflow-notify not installed")
        return

    config = NotifyConfig()
    service = report.get("service", "unknown")
    overall = report.get("overall", "error")
    summary = report.get("summary", "No summary")

    if overall == "pass":
        fanout(
            config=config,
            service=service,
            severity="success",
            message=f"Audit PASSED: {summary}",
        )
    elif overall == "fail":
        # Build detailed failure message
        failures = [s for s in report.get("scenarios", []) if s.get("status") == "fail"]
        failure_details = "; ".join(
            f"Scenario {s.get('id', '?')}: {s.get('observation', 'N/A')}" for s in failures[:3]
        )
        severity = _classify_severity(failures)
        fanout(
            config=config,
            service=service,
            severity=severity,
            message=f"Audit FAILED ({len(failures)} scenario(s)): {failure_details}",
            observation=f"Audit failed: {summary}",
            evidence=failure_details,
            suggested_action="Review audit report and fix failing scenarios",
        )
    elif overall in ("error", "incomplete"):
        fanout(
            config=config,
            service=service,
            severity="warning",
            message=f"Audit {overall.upper()}: {summary}",
            observation=f"Audit {overall}: {summary}",
            evidence=report.get("incomplete_reason", "See report for details"),
            suggested_action="Check auditor logs and re-run",
        )


def _classify_severity(failures: list[dict[str, Any]]) -> str:
    """Classify notification severity based on failure types.

    Service-down or zero-data failures are critical; data quality issues are warnings.
    """
    for f in failures:
        obs = (f.get("observation", "") + f.get("evidence", "")).lower()
        if any(
            kw in obs
            for kw in ["unreachable", "connection refused", "no records", "service down", "0 rows"]
        ):
            return "critical"
    return "warning"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run behavioral auditor against a service",
        prog="workflow-audit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run an audit")
    run_parser.add_argument("--service", required=True, help="Service name (e.g., bid-scraper)")
    run_parser.add_argument("--spec", required=True, help="Path to behavioral spec file")
    run_parser.add_argument("--access", required=True, help="Path to service access document")
    run_parser.add_argument("--mode", default="build", choices=["build", "prod"], help="Audit mode")
    run_parser.add_argument("--model", default="sonnet", help="Claude model (sonnet or opus)")
    run_parser.add_argument("--max-turns", type=int, default=20, help="Max Claude CLI turns")
    run_parser.add_argument("--network", default="dokploy-network", help="Docker network to join")
    run_parser.add_argument("--no-notify", action="store_true", help="Skip notifications")

    build_parser = sub.add_parser("build-image", help="Build the auditor Docker image")
    build_parser.add_argument(
        "--dir",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="Directory containing Dockerfile.auditor",
    )

    args = parser.parse_args()

    if args.command == "build-image":
        ok = build_image(args.dir)
        sys.exit(0 if ok else 1)

    elif args.command == "run":
        report = run_audit(
            spec_path=args.spec,
            access_path=args.access,
            service=args.service,
            mode=args.mode,
            model=args.model,
            max_turns=args.max_turns,
            network=args.network,
            notify=not args.no_notify,
        )
        overall = report.get("overall", "error")
        print(f"\nAudit complete: {overall}")
        if overall in ("fail", "error"):
            sys.exit(1)


if __name__ == "__main__":
    main()
