"""Workflow orchestration CLI: connect spec -> build -> audit -> deploy -> monitor.

Thin glue that chains workflow-env, workflow-agent, git push, and
workflow-notify into a disciplined lifecycle. Human gates at every
irreversible decision point.

Audit runs are delegated to workflow-agent, which resolves roles, policies,
and specs from its own agents/ directory. The orchestrator no longer needs
--spec or --access flags.

Usage:
    workflow-orchestrate build  --service bid-scraper
    workflow-orchestrate deploy --service bid-scraper --repo /path/to/bid-scraper
    workflow-orchestrate monitor --service bid-scraper
    workflow-orchestrate monitor --service defendershield-etl \\
        --exec "python -m ..."
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog

from workflow_platform.config import PlatformConfig
from workflow_platform.workflow_env import cmd_destroy, cmd_up, get_client

log = structlog.get_logger("workflow_platform.orchestrate")


WORKFLOW_AGENT_CLI = Path.home() / "workflow-agent" / ".venv" / "bin" / "workflow-agent"


def _find_report_by_run_id(service: str, run_id: str) -> dict[str, Any] | None:
    """Find a report by exact run_id suffix in the directory name."""
    reports_dir = Path.home() / "agent-output" / service
    if not reports_dir.exists():
        return None

    for d in reports_dir.iterdir():
        if d.is_dir() and d.name.endswith(f"_{run_id}"):
            report_path = d / "report.json"
            if report_path.exists():
                with open(report_path) as f:
                    return json.load(f)
    return None


def _find_report_dir_by_run_id(service: str, run_id: str) -> Path | None:
    """Find a report directory by exact run_id suffix."""
    reports_dir = Path.home() / "agent-output" / service
    if not reports_dir.exists():
        return None

    for d in reports_dir.iterdir():
        if d.is_dir() and d.name.endswith(f"_{run_id}") and (d / "report.json").exists():
            return d
    return None


def _latest_report(
    service: str,
    role: str | None = None,
) -> dict[str, Any] | None:
    """Find the most recent report for a service, optionally filtered by role.

    Directories are named ``{role}_{timestamp}[_{run_id}]`` so we filter
    by prefix when *role* is provided and sort by modification time
    (newest first).

    For deterministic lookup by a specific execution, use
    ``_find_report_by_run_id`` instead.
    """
    reports_dir = Path.home() / "agent-output" / service
    if not reports_dir.exists():
        return None

    candidates = (
        [d for d in reports_dir.iterdir() if d.is_dir() and d.name.startswith(f"{role}_")]
        if role
        else [d for d in reports_dir.iterdir() if d.is_dir()]
    )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for d in candidates:
        report_path = d / "report.json"
        if report_path.exists():
            with open(report_path) as f:
                return json.load(f)
    return None


def _run_workflow_agent(
    service: str,
    role: str = "auditor",
    *,
    model: str = "sonnet",
    max_turns: int = 50,
    timeout: int = 600,
    no_notify: bool = False,
) -> tuple[dict[str, Any], str]:
    """Shell out to workflow-agent CLI to run an agent.

    Returns ``(report_dict, run_id)``.  The run_id is passed to the CLI
    and embedded in the archive directory name so the orchestrator can
    look up the exact report for *this* invocation -- no mtime guessing.
    """
    run_id = uuid.uuid4().hex[:8]

    cmd: list[str] = [
        str(WORKFLOW_AGENT_CLI),
        "run",
        role,
        "--target",
        service,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--timeout",
        str(timeout),
        "--run-id",
        run_id,
        "--no-pull",  # image is always local; GHCR auth not configured on host Docker
    ]
    if no_notify:
        cmd.append("--no-notify")

    log.info(
        "orchestrate.workflow_agent_start",
        service=service,
        role=role,
        model=model,
        run_id=run_id,
        cmd=" ".join(cmd),
    )
    print(f"Running: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 60,  # give CLI overhead beyond container timeout
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # Deterministic lookup: find the report archived with our run_id
    report = _find_report_by_run_id(service, run_id)
    if report is not None:
        return report, run_id

    # Fallback: agent produced no report for this run
    return {
        "overall": "error",
        "service": service,
        "role": role,
        "summary": (
            f"workflow-agent exited {result.returncode} but no report found (run_id={run_id})"
        ),
        "scenarios": [],
    }, run_id


def _push_metrics(service: str, report: dict[str, Any]) -> None:
    """Push agent run metrics to Prometheus Pushgateway (best-effort)."""
    try:
        from workflow_platform.metrics import push_metrics

        role = report.get("role", "auditor")
        push_metrics(service, role, report)
    except ImportError:
        log.warning("orchestrate.metrics_unavailable", reason="prometheus_client not installed")
    except Exception as exc:
        log.warning("orchestrate.metrics_push_failed", service=service, error=str(exc))


def _confirm(prompt: str) -> bool:
    """Ask for human confirmation. Returns True if approved."""
    try:
        resp = input(f"{prompt} [y/N] ")
        return resp.strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False


# -- Build command --


def cmd_build(
    service: str,
    *,
    model: str = "sonnet",
    max_turns: int = 50,
    timeout: int = 600,
    force: bool = False,
) -> dict[str, Any]:
    """Run the build workflow: spin up dev -> run auditor -> present report.

    Returns the audit report dict.
    """
    config = PlatformConfig()
    client = get_client(config)

    print(f"=== Build: {service} ===")
    log.info("orchestrate.build_start", service=service)

    # Step 1: Spin up dev environment
    print("\n--- Step 1: Dev environment ---")
    dev_env = cmd_up(client, config, service, force=force)
    env_id = dev_env.get("environmentId", "unknown")
    print(f"Dev environment ready: {env_id}")

    # Step 2: Run auditor via workflow-agent
    print("\n--- Step 2: Behavioral audit ---")
    report, _run_id = _run_workflow_agent(
        service,
        "auditor",
        model=model,
        max_turns=max_turns,
        timeout=timeout,
    )

    # Step 3: Push metrics
    _push_metrics(service, report)

    # Step 4: Present report
    overall = report.get("overall", "error")
    print(f"\n--- Audit Result: {overall.upper()} ---")

    passed = report.get("scenarios_pass", 0)
    failed = report.get("scenarios_fail", 0)
    errors = report.get("scenarios_error", 0)
    print(f"Scenarios: {passed} pass, {failed} fail, {errors} error")

    if report.get("summary"):
        print(f"Summary: {report['summary']}")

    for s in report.get("scenarios", []):
        status = s.get("status", "?")
        desc = s.get("description", "N/A")
        print(f"  [{status.upper():5s}] {desc}")

    log.info(
        "orchestrate.build_complete",
        service=service,
        overall=overall,
    )

    return report


# -- Deploy command --


def cmd_deploy(
    service: str,
    repo_path: str,
    *,
    branch: str = "main",
    skip_audit_check: bool = False,
) -> bool:
    """Run the deploy workflow: check audit -> confirm -> git push -> notify -> offer teardown.

    Returns True if deployment succeeded.
    """
    config = PlatformConfig()

    print(f"=== Deploy: {service} ===")
    log.info("orchestrate.deploy_start", service=service)

    # Step 1: Verify passing audit report exists
    if not skip_audit_check:
        print("\n--- Step 1: Verify audit ---")
        report = _latest_report(service, role="auditor")
        if report is None:
            print(
                "Error: No audit report found. Run 'workflow-orchestrate build' first.",
                file=sys.stderr,
            )
            return False

        overall = report.get("overall", "error")
        if overall != "pass":
            print(
                f"Error: Latest audit report is '{overall}', not 'pass'. "
                "Fix failing scenarios before deploying.",
                file=sys.stderr,
            )
            return False

        print(f"Latest audit: PASS ({report.get('scenarios_pass', 0)} scenarios)")

    # Step 2: Human confirmation
    print("\n--- Step 2: Human review ---")
    print(f"Service: {service}")
    print(f"Repo: {repo_path}")
    print(f"Branch: {branch}")
    print(f"Action: git push to {branch} (triggers Dokploy auto-deploy)")

    if not _confirm("Deploy to production?"):
        print("Deployment cancelled.")
        log.info("orchestrate.deploy_cancelled", service=service)
        return False

    # Step 3: Git push
    print("\n--- Step 3: Deploy ---")
    if not os.path.isdir(repo_path):
        print(f"Error: Repo not found: {repo_path}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        print(f"Error: git push failed:\n{result.stderr}", file=sys.stderr)
        log.error("orchestrate.push_failed", stderr=result.stderr[:500])
        return False

    print(f"Pushed to origin/{branch}")
    log.info("orchestrate.deployed", service=service, branch=branch)

    # Step 4: Notify
    _send_deploy_notification(service, branch, repo_path)

    # Step 5: Offer teardown
    print("\n--- Step 4: Cleanup ---")
    if _confirm(f"Tear down dev environment for {service}?"):
        try:
            client = get_client(config)
            cmd_destroy(client, config, service)
        except SystemExit:
            print("No dev environment to tear down.")

    return True


def _send_deploy_notification(service: str, branch: str, repo_path: str) -> None:
    """Send a deployment success notification."""
    try:
        from workflow_notify import NotifyConfig, fanout

        config = NotifyConfig()
        # Get the latest commit hash
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        commit = result.stdout.strip() if result.returncode == 0 else "unknown"

        fanout(
            config=config,
            service=service,
            severity="success",
            message=(f"Deployed {service} to production (branch={branch}, commit={commit})"),
        )
        print("Deployment notification sent.")
    except ImportError:
        log.warning("orchestrate.notify_unavailable")
    except Exception as exc:
        log.warning("orchestrate.notify_failed", error=str(exc))


# -- Monitor command --


def _check_container_running(container_name: str) -> bool:
    """Check if a Docker container is running."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _exec_service(
    container_name: str,
    command: str,
    *,
    service: str,
) -> tuple[int, str, str]:
    """Run a command via docker exec on a service container.

    Returns (exit_code, stdout, stderr).
    """
    log.info("orchestrate.exec_start", service=service, container=container_name, command=command)
    print(f"Executing: docker exec {container_name} {command}")

    result = subprocess.run(
        ["docker", "exec", container_name, *command.split()],
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max for long-running ETL jobs
    )

    log.info("orchestrate.exec_complete", service=service, exit_code=result.returncode)
    return result.returncode, result.stdout, result.stderr


def _notify_exec_failure(service: str, exit_code: int, stderr: str) -> None:
    """Send a warning notification for a failed service exec."""
    try:
        from workflow_notify import NotifyConfig, fanout

        fanout(
            config=NotifyConfig(),
            service=service,
            severity="warning",
            message=(
                f"Service exec FAILED for {service} (exit code {exit_code}). Proceeding to audit."
            ),
            observation=f"docker exec exited {exit_code}",
            evidence=stderr[:500] if stderr else "No stderr output",
            suggested_action="Check service logs and re-run manually if needed",
        )
    except ImportError:
        log.warning("orchestrate.notify_unavailable")
    except Exception as exc:
        log.warning("orchestrate.notify_failed", error=str(exc))


def _notify_container_not_running(service: str, container_name: str) -> None:
    """Send a critical notification when the target container is not running."""
    try:
        from workflow_notify import NotifyConfig, fanout

        fanout(
            config=NotifyConfig(),
            service=service,
            severity="critical",
            message=(
                f"CRITICAL: Container '{container_name}' for {service} is not running. "
                f"No exec or audit performed."
            ),
            observation=f"Container {container_name} is stopped or does not exist",
            evidence="docker inspect returned non-running state",
            suggested_action=f"Check container status: docker ps -a | grep {container_name}",
        )
    except ImportError:
        log.warning("orchestrate.notify_unavailable")
    except Exception as exc:
        log.warning("orchestrate.notify_failed", error=str(exc))


def cmd_monitor(
    service: str,
    *,
    exec_command: str | None = None,
    model: str = "sonnet",
    max_turns: int = 50,
    audit_timeout: int = 600,
) -> dict[str, Any]:
    """Run the auditor in prod mode against a live service.

    If exec_command is provided, runs it via docker exec on the service's
    container before auditing. The audit runs regardless of exec outcome
    (unless the container is not running).

    Returns the audit report dict.
    """
    config = PlatformConfig()

    print(f"=== Monitor: {service} ===")
    log.info("orchestrate.monitor_start", service=service, has_exec=exec_command is not None)

    exec_log_content: str | None = None

    # -- Exec phase --
    if exec_command is not None:
        container_name = config.service_containers.get(service)
        if not container_name:
            print(
                f"Error: No container mapping for service '{service}' in config.",
                file=sys.stderr,
            )
            log.error("orchestrate.no_container_mapping", service=service)
            sys.exit(1)

        # Check container is running
        if not _check_container_running(container_name):
            print(f"Error: Container '{container_name}' is not running.", file=sys.stderr)
            log.error("orchestrate.container_not_running", container=container_name)
            _notify_container_not_running(service, container_name)
            sys.exit(1)

        # Execute the service command
        print(f"\n--- Exec: {exec_command} ---")
        exit_code, stdout, stderr = _exec_service(container_name, exec_command, service=service)

        # Buffer exec output to save alongside the report after audit
        exec_log_content = (
            f"=== EXEC: docker exec {container_name} {exec_command} ===\n"
            f"=== EXIT CODE: {exit_code} ===\n\n"
            f"=== STDOUT ===\n{stdout}\n=== STDERR ===\n{stderr}"
        )

        if exit_code != 0:
            print(f"Warning: Service exec failed (exit {exit_code}). Proceeding to audit.")
            _notify_exec_failure(service, exit_code, stderr)
        else:
            print("Service exec completed successfully.")

    # -- Audit phase (delegated to workflow-agent) --
    print("\n--- Audit ---")
    report, run_id = _run_workflow_agent(
        service,
        "auditor",
        model=model,
        max_turns=max_turns,
        timeout=audit_timeout,
    )

    # Save exec output alongside the report if we have it
    if exec_log_content is not None:
        report_dir = _find_report_dir_by_run_id(service, run_id)
        if report_dir is not None:
            exec_log_path = report_dir / "exec_output.log"
            exec_log_path.write_text(exec_log_content)
            log.info("orchestrate.exec_log_saved", path=str(exec_log_path))

    # Push metrics
    _push_metrics(service, report)

    overall = report.get("overall", "error")
    print(f"\nMonitor result: {overall.upper()}")
    log.info("orchestrate.monitor_complete", service=service, overall=overall)

    return report


# -- DAG command --


def _notify_dag_result(service: str, results: dict[str, Any], failed: bool) -> None:
    """Send a notification with the DAG execution result."""
    try:
        from workflow_notify import NotifyConfig, fanout

        summary = ", ".join(f"{k}: {v.value}" for k, v in results.items())

        if failed:
            failed_stages = [k for k, v in results.items() if v.value in ("fail", "error")]
            fanout(
                config=NotifyConfig(),
                service=service,
                severity="warning",
                message=f"DAG FAILED for {service}. Failed stages: {', '.join(failed_stages)}",
                observation=summary,
                suggested_action="Check logs: ~/logs/workflow-monitor.log",
                channel="agent_logs",
            )
        else:
            fanout(
                config=NotifyConfig(),
                service=service,
                severity="success",
                message=f"DAG completed for {service}. All stages passed.",
                observation=summary,
                channel="agent_logs",
            )
    except ImportError:
        log.warning("orchestrate.notify_unavailable")
    except Exception as exc:
        log.warning("orchestrate.notify_failed", error=str(exc))


def cmd_dag(service: str) -> None:
    """Load and execute a DAG for a service. Exits 1 if any stage failed."""
    from workflow_platform.dag import StageResult, execute_dag, load_dag

    log.info("orchestrate.dag_start", service=service)

    try:
        dag = load_dag(service)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        log.error("orchestrate.dag_load_failed", service=service, error=str(exc))
        sys.exit(1)

    results = execute_dag(dag)

    any_failed = any(r in (StageResult.FAIL, StageResult.ERROR) for r in results.values())
    _notify_dag_result(service, results, any_failed)
    if any_failed:
        sys.exit(1)


# -- CLI --


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Workflow lifecycle orchestration",
        prog="workflow-orchestrate",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Build
    build_p = sub.add_parser("build", help="Spin up dev, run auditor, present report")
    build_p.add_argument("--service", required=True, help="Service name")
    build_p.add_argument("--model", default="sonnet", help="Claude model")
    build_p.add_argument("--max-turns", type=int, default=50, help="Max auditor turns")
    build_p.add_argument(
        "--audit-timeout",
        type=int,
        default=600,
        help="Max seconds for auditor container (default: 600)",
    )
    build_p.add_argument("--force", action="store_true", help="Skip resource guard")

    # Deploy
    deploy_p = sub.add_parser("deploy", help="Verify audit, confirm, push, notify")
    deploy_p.add_argument("--service", required=True, help="Service name")
    deploy_p.add_argument("--repo", required=True, help="Path to git repo")
    deploy_p.add_argument("--branch", default="main", help="Branch to push")
    deploy_p.add_argument(
        "--skip-audit-check",
        action="store_true",
        help="Skip audit report verification (not recommended)",
    )

    # Briefing
    briefing_p = sub.add_parser(
        "briefing", help="Run a daily briefing cycle: gather -> synthesize -> post"
    )
    briefing_p.add_argument(
        "mode",
        choices=["morning", "consolidate", "weekly"],
        help="Briefing mode",
    )

    # DAG
    dag_p = sub.add_parser("dag", help="Execute a YAML-defined DAG for a service")
    dag_p.add_argument("service", help="Service name (matches dags/<service>.yaml)")

    # Monitor
    mon_p = sub.add_parser(
        "monitor", help="Run auditor in prod mode (optionally exec service first)"
    )
    mon_p.add_argument("--service", required=True, help="Service name")
    mon_p.add_argument(
        "--exec",
        dest="exec_command",
        default=None,
        help="Command to run via docker exec before auditing (e.g., 'python -m my_module run')",
    )
    mon_p.add_argument(
        "--audit-timeout",
        type=int,
        default=600,
        help="Max seconds for auditor container (default: 600 = 10 min)",
    )
    mon_p.add_argument("--model", default="sonnet", help="Claude model")
    mon_p.add_argument("--max-turns", type=int, default=50, help="Max auditor turns")

    args = parser.parse_args()

    if args.command == "build":
        report = cmd_build(
            args.service,
            model=args.model,
            max_turns=args.max_turns,
            timeout=args.audit_timeout,
            force=args.force,
        )
        if report.get("overall") in ("fail", "error"):
            sys.exit(1)

    elif args.command == "deploy":
        ok = cmd_deploy(
            args.service,
            args.repo,
            branch=args.branch,
            skip_audit_check=args.skip_audit_check,
        )
        if not ok:
            sys.exit(1)

    elif args.command == "dag":
        cmd_dag(args.service)

    elif args.command == "monitor":
        report = cmd_monitor(
            args.service,
            exec_command=args.exec_command,
            model=args.model,
            max_turns=args.max_turns,
            audit_timeout=args.audit_timeout,
        )
        if report.get("overall") in ("fail", "error"):
            sys.exit(1)

    elif args.command == "briefing":
        from workflow_platform.briefing import cmd_briefing

        ok = cmd_briefing(args.mode)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
