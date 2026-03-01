"""Infrastructure health checks and boot notifications.

Deterministic checks for disk, memory, and container status. All
notifications route through workflow-notify fanout. Replaces the legacy
/home/docker/monitoring/ scripts.

Usage:
    workflow-health check    # Weekly infra health check
    workflow-health boot     # Post-boot container inventory (5-min delay)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import structlog

log = structlog.get_logger("workflow_platform.health")

# Containers that should be running in steady state.
# Update this list when services are added or removed.
# Names are matched as prefixes so Docker Swarm suffixes
# (e.g. "dokploy.1.k8u2c7n14id8") still match.
EXPECTED_CONTAINERS: list[str] = [
    "dokploy",
    "dokploy-postgres",
    "dokploy-redis",
    "dokploy-traefik",
    "cloudflared",
    "open-webui",
    "etl-postgres",
    "etl-scheduler",
    "n8n",
    "n8n-postgres",
    "bid-scraper-postgres",
    "discord-capture-bot",
    "monitoring-cadvisor",
    "monitoring-grafana",
    "monitoring-node-exporter",
    "monitoring-prometheus",
    "workflow-sentinel",
    "crowdsec",
    "dozzle",
    "homepage",
]

# Thresholds -- generous, only flag obvious problems.
DISK_WARN_PERCENT = 85
MEMORY_WARN_PERCENT = 90

# Boot stabilization delay in seconds.
BOOT_DELAY_SECONDS = 300  # 5 minutes


def _find_container_status(expected: str, statuses: dict[str, str]) -> str:
    """Find status for an expected container using flexible name matching.

    Docker Swarm appends suffixes (``dokploy.1.k8u2c7n14id8``).
    We try three strategies in order: exact match, prefix match,
    then substring match.
    """
    if expected in statuses:
        return statuses[expected]
    for actual_name, status in statuses.items():
        if actual_name.startswith(expected):
            return status
    for actual_name, status in statuses.items():
        if expected in actual_name:
            return status
    return ""


def _get_disk_usage() -> list[dict[str, str | float]]:
    """Return mount points with usage percentage.

    Only checks real filesystems (excludes tmpfs, devtmpfs, etc.).
    """
    result = subprocess.run(
        [
            "df",
            "-h",
            "--output=target,pcent",
            "-x",
            "tmpfs",
            "-x",
            "devtmpfs",
            "-x",
            "squashfs",
            "-x",
            "overlay",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    mounts: list[dict[str, str | float]] = []
    if result.returncode != 0:
        return mounts
    for line in result.stdout.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            mount = parts[0]
            pct = float(parts[-1].rstrip("%"))
            mounts.append({"mount": mount, "percent": pct})
    return mounts


def _get_memory_usage() -> float:
    """Return memory usage as a percentage."""
    result = subprocess.run(
        ["free", "-m"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return -1.0
    for line in result.stdout.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            total = float(parts[1])
            used = float(parts[2])
            if total > 0:
                return round(used / total * 100, 1)
    return -1.0


def _get_container_statuses() -> dict[str, str]:
    """Return a mapping of container name -> status string for all containers."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    statuses: dict[str, str] = {}
    if result.returncode != 0:
        return statuses
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            statuses[parts[0]] = parts[1]
    return statuses


def _is_docker_available() -> bool:
    """Check if the Docker daemon is reachable."""
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


def _notify(severity: str, message: str) -> None:
    """Send a notification through workflow-notify fanout."""
    try:
        from workflow_notify import NotifyConfig, fanout

        fanout(
            config=NotifyConfig(),
            service="infrastructure",
            severity=severity,
            message=message,
        )
        log.info("health.notification_sent", severity=severity)
    except ImportError:
        log.error("health.workflow_notify_unavailable")
    except Exception as exc:
        log.error("health.notification_failed", error=str(exc))


def cmd_check() -> None:
    """Run deterministic infrastructure health check.

    Checks disk usage, memory usage, and expected container status.
    Sends a single notification summarizing all findings.
    """
    log.info("health.check_start")
    findings: list[str] = []

    # Check Docker daemon
    if not _is_docker_available():
        msg = "Docker daemon is unreachable."
        log.error("health.docker_unavailable")
        _notify("critical", f"Infrastructure health check FAILED: {msg}")
        print(f"CRITICAL: {msg}", file=sys.stderr)
        sys.exit(1)

    # Check disk usage
    try:
        mounts = _get_disk_usage()
        disk_summary_parts: list[str] = []
        for m in mounts:
            pct = m["percent"]
            mount = m["mount"]
            if isinstance(pct, float) and pct >= DISK_WARN_PERCENT:
                findings.append(f"Disk {mount} at {pct:.0f}%")
            if isinstance(pct, float):
                disk_summary_parts.append(f"{mount}: {pct:.0f}%")
        disk_summary = ", ".join(disk_summary_parts) if disk_summary_parts else "unknown"
    except Exception as exc:
        findings.append(f"Disk check failed: {exc}")
        disk_summary = "error"

    # Check memory usage
    try:
        mem_pct = _get_memory_usage()
        if mem_pct < 0:
            findings.append("Memory check failed")
            mem_summary = "error"
        elif mem_pct >= MEMORY_WARN_PERCENT:
            findings.append(f"Memory at {mem_pct}%")
            mem_summary = f"{mem_pct}%"
        else:
            mem_summary = f"{mem_pct}%"
    except Exception as exc:
        findings.append(f"Memory check failed: {exc}")
        mem_summary = "error"

    # Check expected containers
    container_statuses = _get_container_statuses()
    running_count = 0
    missing: list[str] = []
    for name in EXPECTED_CONTAINERS:
        status = _find_container_status(name, container_statuses)
        if status.startswith("Up"):
            running_count += 1
        else:
            missing.append(name)

    if missing:
        findings.append(f"Missing containers: {', '.join(missing)}")

    container_summary = f"{running_count}/{len(EXPECTED_CONTAINERS)} expected containers running"

    # Build and send notification
    if findings:
        severity = "warning"
        body = f"Infrastructure health check: {len(findings)} issue(s) found.\n"
        body += "\n".join(f"- {f}" for f in findings)
        body += f"\n\nDisk: {disk_summary}. Memory: {mem_summary}. {container_summary}."
    else:
        severity = "success"
        body = (
            f"Infrastructure healthy. "
            f"{container_summary}. Disk: {disk_summary}. Memory: {mem_summary}."
        )

    _notify(severity, body)
    print(body)
    log.info("health.check_complete", severity=severity, findings_count=len(findings))


def cmd_boot() -> None:
    """Post-boot container inventory notification.

    Waits for Docker to be available (up to 5 minutes), then reports
    container status through workflow-notify.
    """
    log.info("health.boot_start", delay_seconds=BOOT_DELAY_SECONDS)
    print(f"Waiting {BOOT_DELAY_SECONDS}s for system stabilization...")

    # Wait for Docker daemon, checking periodically during the delay
    deadline = time.monotonic() + BOOT_DELAY_SECONDS
    docker_ready = False
    while time.monotonic() < deadline:
        if _is_docker_available():
            docker_ready = True
        time.sleep(30)

    if not docker_ready:
        msg = "Docker daemon not available after 5-minute boot delay."
        log.error("health.boot_docker_unavailable")
        try:
            _notify("critical", f"Boot notification FAILED: {msg}")
        except Exception:
            pass
        print(f"CRITICAL: {msg}", file=sys.stderr)
        sys.exit(1)

    # Get container inventory
    container_statuses = _get_container_statuses()
    running = []
    stopped = []
    for name in sorted(container_statuses.keys()):
        status = container_statuses[name]
        if status.startswith("Up"):
            running.append(name)
        else:
            stopped.append(f"{name} ({status})")

    total = len(container_statuses)
    running_count = len(running)

    # Check if expected containers are present
    missing_expected = [
        name
        for name in EXPECTED_CONTAINERS
        if not _find_container_status(name, container_statuses).startswith("Up")
    ]

    # Build message
    lines = [f"System boot complete. {running_count}/{total} containers running."]
    if missing_expected:
        lines.append(f"\nExpected but not running: {', '.join(missing_expected)}")

    lines.append("\nContainer status:")
    for name in sorted(container_statuses.keys()):
        status = container_statuses[name]
        marker = "[UP]" if status.startswith("Up") else "[DOWN]"
        lines.append(f"  {marker} {name}")

    body = "\n".join(lines)

    if missing_expected:
        severity = "warning"
    else:
        severity = "success"

    _notify(severity, body)
    print(body)
    log.info(
        "health.boot_complete",
        severity=severity,
        running=running_count,
        total=total,
        missing_expected=len(missing_expected),
    )


def main() -> None:
    """CLI entry point for workflow-health."""
    parser = argparse.ArgumentParser(
        description="Infrastructure health checks and boot notifications",
        prog="workflow-health",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Run weekly infrastructure health check")
    sub.add_parser("boot", help="Post-boot container inventory (5-min delay)")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check()
    elif args.command == "boot":
        cmd_boot()


if __name__ == "__main__":
    main()
