"""workflow-env CLI: manage dev/prod environments in Dokploy.

Commands:
    workflow-env up <service>       Duplicate prod environment for dev
    workflow-env down <service>     Stop dev containers (preserve volumes)
    workflow-env destroy <service>  Remove dev environment entirely
    workflow-env list               Show active dev environments
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from typing import Any

import structlog

from workflow_platform.config import PlatformConfig
from workflow_platform.dokploy_client import DokployClient, DokployError

log = structlog.get_logger("workflow_platform.workflow_env")

DEV_ENV_PREFIX = "dev-"


def get_client(config: PlatformConfig) -> DokployClient:
    """Create a DokployClient from config."""
    if not config.dokploy_api_key:
        print("Error: DOKPLOY_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    return DokployClient(config.dokploy_url, config.dokploy_api_key)


def _find_service_in_env(env: dict[str, Any], service_name: str) -> tuple[str, str] | None:
    """Find a service by name in an environment.

    Returns (service_id, service_type) or None.
    """
    for compose in env.get("compose", []):
        if compose["name"] == service_name:
            return (compose["composeId"], "compose")
    for app in env.get("applications", []):
        if app["name"] == service_name:
            return (app["applicationId"], "application")
    return None


def _get_prod_env(project: dict[str, Any], prod_env_id: str) -> dict[str, Any] | None:
    """Find the production environment in the project."""
    for env in project.get("environments", []):
        if env["environmentId"] == prod_env_id:
            return env
    return None


def _find_dev_env(project: dict[str, Any], service_name: str) -> dict[str, Any] | None:
    """Find an existing dev environment for a service."""
    target_name = f"{DEV_ENV_PREFIX}{service_name}"
    for env in project.get("environments", []):
        if env["name"] == target_name:
            return env
    return None


def _get_dev_envs(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Get all dev environments (those prefixed with DEV_ENV_PREFIX)."""
    return [
        env for env in project.get("environments", []) if env["name"].startswith(DEV_ENV_PREFIX)
    ]


# -- Resource guard --


def check_resources(config: PlatformConfig) -> list[str]:
    """Check if host resources are within safe limits.

    Returns a list of warning messages. Empty = all clear.
    """
    warnings: list[str] = []

    # Container count
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_count = len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0
        if container_count >= config.max_containers:
            warnings.append(
                f"Container count ({container_count}) at or above limit ({config.max_containers})"
            )
    except Exception as exc:
        warnings.append(f"Could not check container count: {exc}")

    # Free RAM
    if shutil.which("free"):
        try:
            result = subprocess.run(
                ["free", "-m"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                if line.startswith("Mem:"):
                    parts = line.split()
                    available = int(parts[6])  # "available" column
                    if available < config.min_free_ram_mb:
                        warnings.append(
                            f"Available RAM ({available}MB) below minimum"
                            f" ({config.min_free_ram_mb}MB)"
                        )
                    break
        except Exception as exc:
            warnings.append(f"Could not check RAM: {exc}")

    return warnings


# -- Commands --


def cmd_up(
    client: DokployClient,
    config: PlatformConfig,
    service_name: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create a dev environment for a service by duplicating prod.

    Returns the new environment dict.
    """
    project = client.get_project(config.dokploy_project_id)

    # Check if dev env already exists
    existing = _find_dev_env(project, service_name)
    if existing:
        print(f"Dev environment already exists: {existing['name']} ({existing['environmentId']})")
        return existing

    # Find service in prod
    prod_env = _get_prod_env(project, config.dokploy_prod_env_id)
    if prod_env is None:
        print("Error: Production environment not found.", file=sys.stderr)
        sys.exit(1)

    svc = _find_service_in_env(prod_env, service_name)
    if svc is None:
        available = [c["name"] for c in prod_env.get("compose", [])] + [
            a["name"] for a in prod_env.get("applications", [])
        ]
        print(f"Error: Service '{service_name}' not found in prod.", file=sys.stderr)
        print(f"Available: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    svc_id, svc_type = svc

    # Resource guard
    if not force:
        warnings = check_resources(config)
        if warnings:
            print("Resource warnings:")
            for w in warnings:
                print(f"  - {w}")
            resp = input("Continue anyway? [y/N] ")
            if resp.lower() != "y":
                print("Aborted.")
                sys.exit(0)

    # Duplicate environment with just this service
    dev_name = f"{DEV_ENV_PREFIX}{service_name}"
    log.info("workflow_env.duplicating", service=service_name, dev_name=dev_name)

    new_env = client.duplicate_environment(
        source_env_id=config.dokploy_prod_env_id,
        name=dev_name,
        include_services=True,
        selected_services=[{"id": svc_id, "type": svc_type}],
    )

    env_id = new_env["environmentId"]
    print(f"Dev environment created: {dev_name} ({env_id})")

    # Apply dev overrides to the duplicated service
    _apply_dev_overrides(client, config, new_env, service_name)

    log.info("workflow_env.up_complete", service=service_name, env_id=env_id)
    return new_env


def _apply_dev_overrides(
    client: DokployClient,
    config: PlatformConfig,
    dev_env: dict[str, Any],
    service_name: str,
) -> None:
    """Apply dev-specific config overrides to duplicated services.

    Modifies compose files to use dev-specific hostnames, preventing
    DNS collisions on the shared dokploy-network.
    """
    # Refresh the project to get the new environment's services
    project = client.get_project(config.dokploy_project_id)
    env_id = dev_env["environmentId"]

    for env in project.get("environments", []):
        if env["environmentId"] != env_id:
            continue

        for compose in env.get("compose", []):
            compose_file = compose.get("composeFile", "")
            if not compose_file:
                continue

            # Replace prod hostnames with dev hostnames in compose file
            updated = _rewrite_compose_for_dev(compose_file, service_name)
            if updated != compose_file:
                try:
                    client.update_compose(compose["composeId"], composeFile=updated)
                    log.info(
                        "workflow_env.compose_updated",
                        compose_id=compose["composeId"],
                        service=service_name,
                    )
                except DokployError as exc:
                    log.warning(
                        "workflow_env.compose_update_failed",
                        error=str(exc),
                        compose_id=compose["composeId"],
                    )


def _rewrite_compose_for_dev(compose_file: str, service_name: str) -> str:
    """Rewrite a compose file to use dev-specific hostnames.

    Replaces `hostname: X-postgres` with `hostname: X-dev-postgres`
    and updates DATABASE_URL references accordingly.
    """
    # Replace hostname directives (e.g., bid-scraper-postgres -> bid-scraper-dev-postgres)
    result = re.sub(
        r"(hostname:\s*)(\S+-postgres)",
        lambda m: f"{m.group(1)}{m.group(2).replace('-postgres', '-dev-postgres')}",
        compose_file,
    )

    # Replace DB host references in environment vars
    result = re.sub(
        r"(@|DB_POSTGRESDB_HOST=|DB_HOST=)(\S+-postgres)",
        lambda m: f"{m.group(1)}{m.group(2).replace('-postgres', '-dev-postgres')}",
        result,
    )

    # Make volume names non-external (dev gets its own volumes, not prod's)
    result = re.sub(
        r"(\s+)external:\s*true\n\s+name:\s*\S+",
        r"\1# dev: using local volume (not prod's external volume)",
        result,
    )

    return result


def cmd_down(client: DokployClient, config: PlatformConfig, service_name: str) -> None:
    """Stop containers in a dev environment."""
    project = client.get_project(config.dokploy_project_id)
    dev_env = _find_dev_env(project, service_name)

    if dev_env is None:
        print(f"No dev environment found for '{service_name}'.", file=sys.stderr)
        sys.exit(1)

    env_id = dev_env["environmentId"]

    # Find all services in the dev environment and stop them
    for env in project.get("environments", []):
        if env["environmentId"] != env_id:
            continue

        for compose in env.get("compose", []):
            try:
                client.stop_compose(compose["composeId"])
                print(f"Stopped compose: {compose['name']}")
            except DokployError as exc:
                log.warning("workflow_env.stop_failed", error=str(exc))

        for app in env.get("applications", []):
            try:
                client.stop_application(app["applicationId"])
                print(f"Stopped application: {app['name']}")
            except DokployError as exc:
                log.warning("workflow_env.stop_failed", error=str(exc))

    print(f"Dev environment '{DEV_ENV_PREFIX}{service_name}' stopped.")


def cmd_destroy(client: DokployClient, config: PlatformConfig, service_name: str) -> None:
    """Destroy a dev environment entirely."""
    project = client.get_project(config.dokploy_project_id)
    dev_env = _find_dev_env(project, service_name)

    if dev_env is None:
        print(f"No dev environment found for '{service_name}'.", file=sys.stderr)
        sys.exit(1)

    env_id = dev_env["environmentId"]
    client.remove_environment(env_id)
    print(f"Dev environment '{DEV_ENV_PREFIX}{service_name}' destroyed ({env_id}).")


def cmd_list(client: DokployClient, config: PlatformConfig) -> list[dict[str, Any]]:
    """List all dev environments."""
    project = client.get_project(config.dokploy_project_id)
    dev_envs = _get_dev_envs(project)

    if not dev_envs:
        print("No dev environments found.")
        return []

    results = []
    for env in dev_envs:
        service_name = env["name"].removeprefix(DEV_ENV_PREFIX)
        env_id = env["environmentId"]

        # Count services
        svc_count = 0
        for full_env in project.get("environments", []):
            if full_env["environmentId"] == env_id:
                svc_count = len(full_env.get("compose", [])) + len(full_env.get("applications", []))
                break

        info = {
            "service": service_name,
            "env_id": env_id,
            "name": env["name"],
            "services": svc_count,
            "created": env.get("createdAt", "unknown"),
        }
        results.append(info)
        print(f"  {service_name:20s}  {env_id}  services={svc_count}  created={info['created']}")

    return results


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Manage dev/prod environments in Dokploy",
        prog="workflow-env",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up_parser = sub.add_parser("up", help="Create a dev environment for a service")
    up_parser.add_argument("service", help="Service name (e.g., bid-scraper)")
    up_parser.add_argument("--force", action="store_true", help="Skip resource guard")

    down_parser = sub.add_parser("down", help="Stop dev containers")
    down_parser.add_argument("service", help="Service name")

    destroy_parser = sub.add_parser("destroy", help="Remove dev environment entirely")
    destroy_parser.add_argument("service", help="Service name")

    sub.add_parser("list", help="Show active dev environments")

    args = parser.parse_args()
    config = PlatformConfig()
    client = get_client(config)

    if args.command == "up":
        cmd_up(client, config, args.service, force=args.force)
    elif args.command == "down":
        cmd_down(client, config, args.service)
    elif args.command == "destroy":
        cmd_destroy(client, config, args.service)
    elif args.command == "list":
        cmd_list(client, config)


if __name__ == "__main__":
    main()
