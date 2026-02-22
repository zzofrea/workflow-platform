# Monitoring Consolidation Spec

## System Overview

Tear down the legacy monitoring stack (`/home/docker/monitoring/`) and its systemd units, replacing its two useful capabilities -- weekly infrastructure health checks and boot notifications -- with deterministic scripts routed through the workflow platform's existing notification hub (`workflow-notify`). The Docker event watcher is dropped entirely.

## Behavioral Contract

### Teardown

- When the migration is complete, the directory `/home/docker/monitoring/` no longer exists.
- When the migration is complete, the systemd units `weekly-health-report.service`, `weekly-health-report.timer`, `boot-notify.service`, and `docker-watcher.service` no longer exist and are not loaded.
- When the migration is complete, no scripts reference the legacy Discord webhook URL directly -- all notifications route through `workflow-notify`.

### Weekly Infrastructure Health Check

- When Sunday 9 AM ET arrives, the system runs a deterministic health check that inspects disk usage, memory usage, and container status.
- When disk usage on any mount exceeds 85%, the check reports a warning identifying the mount and usage percentage.
- When memory usage exceeds 90%, the check reports a warning with current usage.
- When any of the expected containers are not running, the check reports a warning listing the missing containers.
- When all checks pass, the system sends a single "success" notification summarizing the healthy state (e.g., "All 14 containers running. Disk: 42%. Memory: 61%.").
- When any check fails, the system sends a "warning" notification listing all findings.
- When the health check itself errors (e.g., Docker daemon unreachable), the system sends a "critical" notification.

### Boot Notification

- When the system boots, the notification is sent after a 5-minute stabilization delay (not immediately).
- When the boot notification fires, it reports: container count (running vs total) and a per-container status list (name + up/down).
- When all expected containers are running, the notification severity is "success".
- When one or more expected containers are not running, the notification severity is "warning".

### Notification Routing

- When a health check or boot notification fires, it uses `workflow-notify` fanout with the appropriate severity level, following the existing routing rules:
  - **critical**: Discord + Email + Vault
  - **warning**: Discord + Vault
  - **success**: Discord only

## Explicit Non-Behaviors

- The system must NOT use Claude CLI or any AI model for health checks, because deterministic threshold checks are cheaper, faster, and more reliable for infrastructure monitoring.
- The system must NOT send raw Discord webhook calls from monitoring scripts, because all notifications must route through `workflow-notify` for consistent fanout and audit trail.
- The system must NOT monitor Docker events (start/die/OOM) in real-time, because this capability is being dropped per decision.
- The system must NOT duplicate the behavioral auditor's per-service checks, because infrastructure health (disk/memory/containers) and service behavior (data freshness/correctness) are separate concerns handled by separate systems.

## Integration Boundaries

### workflow-notify (Notification Hub)
- **In:** Python function call -- `fanout(config, service="infrastructure", severity=..., message=...)` or CLI invocation.
- **Out:** Routed to Discord, Email, and/or Vault per severity rules.
- **Unavailable:** Log the failure locally. The user will be hands-on during reboots anyway.

### Docker Daemon
- **In:** `docker ps` for container inventory, `docker info` for daemon health.
- **Out:** Container names, statuses, counts.
- **Unavailable:** Health check reports critical notification. Boot notification retries daemon availability before the 5-minute window expires.

### Host System
- **In:** `df` for disk usage, `free` for memory usage.
- **Out:** Usage percentages.
- **Unavailable:** If these commands fail, something is catastrophically wrong and the script will error out naturally.

### systemd (Scheduling)
- **In:** Timer unit triggers weekly health check. Boot-after-Docker target triggers boot notification.
- **Out:** None.
- **Contract:** Replaces existing systemd units with new ones pointing to the workflow-platform scripts.

## Behavioral Scenarios

### Happy Path

```
; Weekly health check finds no issues.
GIVEN the system has been running normally for a week.
WHEN the weekly health check runs on Sunday at 9 AM ET.
THEN a single Discord message appears with severity "success" containing disk usage percentage, memory usage percentage, and container count.
AND no email is sent.
AND no vault file is created.
```

```
; Clean boot with all containers up.
GIVEN the system has just rebooted and all Docker containers start successfully.
WHEN 5 minutes have elapsed since boot.
THEN a Discord message appears listing all containers as running with severity "success".
AND no email is sent.
```

```
; Health check catches disk pressure.
GIVEN one mount point is at 87% disk usage.
WHEN the weekly health check runs.
THEN a Discord message appears with severity "warning" identifying the mount and its 87% usage.
AND a vault monitoring file is created with the same finding.
AND no email is sent (warning, not critical).
```

### Error Scenarios

```
; Boot with missing containers.
GIVEN the system has rebooted but 2 containers failed to start.
WHEN 5 minutes have elapsed since boot.
THEN a Discord message appears with severity "warning" listing the 2 missing containers by name and showing the remaining containers as running.
AND a vault monitoring file is created with the container inventory.
```

```
; Health check cannot reach Docker daemon.
GIVEN the Docker daemon is unresponsive.
WHEN the weekly health check runs.
THEN a notification fires with severity "critical" stating the Docker daemon is unreachable.
AND an email is sent (critical severity).
AND a vault monitoring file is created.
```

### Edge Cases

```
; Multiple threshold breaches in one health check.
GIVEN disk is at 88% AND memory is at 92% AND 1 container is down.
WHEN the weekly health check runs.
THEN a single notification fires with severity "warning" listing all three findings in one message (not three separate notifications).
```

```
; Boot notification when workflow-notify is unavailable.
GIVEN the system has just rebooted and the workflow-notify Python environment is not yet available.
WHEN the boot notification script runs.
THEN the error is logged to a local file.
AND no notification is sent (no fallback to raw Discord webhook).
```

## Resolved Ambiguities

1. **Expected container list**: Config-driven. The list of expected containers lives in `workflow-platform`'s config module (e.g., `config.py` or a dedicated `health.py` config). New services are added to the list when deployed.

2. **Boot notification trigger mechanism**: 5-minute `sleep` in the script itself. Simple, no extra systemd units. The `boot-notify.service` unit calls the new Python entry point, which sleeps before checking.

3. **Script location**: Python modules in `workflow-platform/src/workflow_platform/` with CLI entry points in `pyproject.toml`, consistent with `workflow-env`, `workflow-audit`, `workflow-orchestrate`.

4. **Scheduling**: Crontab for the weekly health check, consistent with the rest of the workflow platform's scheduling. The boot notification remains a systemd service (triggered by boot target), but the script it calls is the new Python entry point. The legacy systemd timer for weekly health is removed.

## Implementation Constraints

- Python, consistent with the rest of workflow-platform (type hints, Google-style docstrings, PEP 8).
- Must use `workflow-notify` for all notification delivery -- no direct webhook calls.
- New CLI entry point(s) registered in `pyproject.toml`, consistent with existing `workflow-env`, `workflow-audit`, `workflow-orchestrate`.
- Teardown must remove all legacy artifacts: scripts, systemd units, and disable/stop the units before deletion.
- Boot notification must tolerate a partially-started system (Docker may still be pulling images or starting containers at the 5-minute mark).
