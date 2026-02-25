"""Workflow orchestration CLI: connect spec -> build -> audit -> deploy -> monitor.

Thin glue that chains workflow-env, workflow-audit, git push, and
workflow-notify into a disciplined lifecycle. Human gates at every
irreversible decision point.

Usage:
    workflow-orchestrate build  --service bid-scraper --spec spec.md --access access.md
    workflow-orchestrate deploy --service bid-scraper --repo /home/docker/gov-bid-scrape
    workflow-orchestrate monitor --service bid-scraper --spec spec.md --access access.md
    workflow-orchestrate monitor --service defendershield-etl \\
        --exec "python -m ..." --spec spec.md --access access.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog

from workflow_platform.auditor import _report_archive_dir
from workflow_platform.config import PlatformConfig
from workflow_platform.auditor import run_audit_v2
from workflow_platform.workflow_env import cmd_destroy, cmd_up, get_client

log = structlog.get_logger("workflow_platform.orchestrate")


def _latest_report(service: str) -> dict[str, Any] | None:
    """Find the most recent audit report for a service."""
    reports_dir = Path.home() / "audit-reports" / service
    if not reports_dir.exists():
        return None

    # Reports are stored as {mode}_{timestamp}/ dirs -- sort to get latest
    subdirs = sorted(reports_dir.iterdir(), reverse=True)
    for d in subdirs:
        report_path = d / "report.json"
        if report_path.exists():
            with open(report_path) as f:
                return json.load(f)
    return None


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
    spec_path: str,
    access_path: str,
    *,
    model: str = "sonnet",
    max_turns: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    """Run the build workflow: spin up dev -> run auditor -> present report.

    Returns the audit report dict.
    """
    config = PlatformConfig()
    client = get_client(config)

    # Step 1: Verify spec exists
    if not os.path.isfile(spec_path):
        print(f"Error: Spec not found: {spec_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(access_path):
        print(f"Error: Access doc not found: {access_path}", file=sys.stderr)
        sys.exit(1)

    print(f"=== Build: {service} ===")
    log.info("orchestrate.build_start", service=service)

    # Step 2: Spin up dev environment
    print("\n--- Step 1: Dev environment ---")
    dev_env = cmd_up(client, config, service, force=force)
    env_id = dev_env.get("environmentId", "unknown")
    print(f"Dev environment ready: {env_id}")

    # Step 3: Run auditor against dev
    print("\n--- Step 2: Behavioral audit ---")
    report = run_audit_v2(
        spec_path=spec_path,
        access_path=access_path,
        service=service,
        mode="build",
        model=model,
        max_turns=max_turns,
        notify=True,
    )

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
        report = _latest_report(service)
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
    spec_path: str,
    access_path: str,
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

    # Resolve archive dir early so exec output can be saved alongside the report
    archive_dir = _report_archive_dir(service, "prod")
    os.makedirs(archive_dir, exist_ok=True)

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

        # Save exec output to archive
        with open(os.path.join(archive_dir, "exec_output.log"), "w") as f:
            f.write(f"=== EXEC: docker exec {container_name} {exec_command} ===\n")
            f.write(f"=== EXIT CODE: {exit_code} ===\n\n")
            f.write("=== STDOUT ===\n")
            f.write(stdout)
            f.write("\n=== STDERR ===\n")
            f.write(stderr)

        if exit_code != 0:
            print(f"Warning: Service exec failed (exit {exit_code}). Proceeding to audit.")
            _notify_exec_failure(service, exit_code, stderr)
        else:
            print("Service exec completed successfully.")

    # -- Audit phase --
    print("\n--- Audit ---")
    report = run_audit_v2(
        spec_path=spec_path,
        access_path=access_path,
        service=service,
        mode="prod",
        model=model,
        max_turns=max_turns,
        notify=True,
        total_timeout=audit_timeout,
        archive_dir=archive_dir,
    )

    overall = report.get("overall", "error")
    print(f"\nMonitor result: {overall.upper()}")
    log.info("orchestrate.monitor_complete", service=service, overall=overall)

    return report


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
    build_p.add_argument("--spec", required=True, help="Path to behavioral spec")
    build_p.add_argument("--access", required=True, help="Path to access document")
    build_p.add_argument("--model", default="sonnet", help="Claude model")
    build_p.add_argument("--max-turns", type=int, default=50, help="Max auditor turns")
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

    # Monitor
    mon_p = sub.add_parser(
        "monitor", help="Run auditor in prod mode (optionally exec service first)"
    )
    mon_p.add_argument("--service", required=True, help="Service name")
    mon_p.add_argument("--spec", required=True, help="Path to behavioral spec")
    mon_p.add_argument("--access", required=True, help="Path to access document")
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
            args.spec,
            args.access,
            model=args.model,
            max_turns=args.max_turns,
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

    elif args.command == "monitor":
        report = cmd_monitor(
            args.service,
            args.spec,
            args.access,
            exec_command=args.exec_command,
            model=args.model,
            max_turns=args.max_turns,
            audit_timeout=args.audit_timeout,
        )
        if report.get("overall") in ("fail", "error"):
            sys.exit(1)


if __name__ == "__main__":
    main()
