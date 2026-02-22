"""HTTP client for the Dokploy tRPC API.

Wraps the tRPC query/mutation pattern into simple Python calls.
All methods are synchronous (httpx). Errors raise DokployError.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx
import structlog

log = structlog.get_logger("workflow_platform.dokploy_client")


class DokployError(Exception):
    """Raised when a Dokploy API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DokployClient:
    """Thin client for Dokploy tRPC API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _query(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tRPC query (GET)."""
        encoded = urllib.parse.quote(json.dumps({"json": params}))
        url = f"{self.base_url}/api/trpc/{path}?input={encoded}"
        resp = httpx.get(url, headers=self._headers(), timeout=self.timeout)
        return self._handle_response(resp, path)

    def _mutation(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tRPC mutation (POST)."""
        url = f"{self.base_url}/api/trpc/{path}"
        resp = httpx.post(
            url,
            headers=self._headers(),
            json={"json": params},
            timeout=self.timeout,
        )
        return self._handle_response(resp, path)

    def _handle_response(self, resp: httpx.Response, path: str) -> dict[str, Any]:
        """Extract the JSON result or raise DokployError."""
        try:
            data = resp.json()
        except Exception:
            raise DokployError(
                f"{path}: HTTP {resp.status_code}, non-JSON response",
                status_code=resp.status_code,
            )

        if "error" in data:
            err = data["error"]
            msg = err.get("json", {}).get("message", str(err))
            code = err.get("json", {}).get("data", {}).get("httpStatus", resp.status_code)
            raise DokployError(f"{path}: {msg}", status_code=code)

        # tRPC wraps results in result.data.json
        return data.get("result", {}).get("data", {}).get("json", data)

    # -- Project / Environment --

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a project with all its environments and services."""
        return self._query("project.one", {"projectId": project_id})

    def duplicate_environment(
        self,
        source_env_id: str,
        name: str,
        *,
        include_services: bool = True,
        selected_services: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Duplicate an environment within the same project.

        Args:
            source_env_id: The production environment ID to duplicate from.
            name: Name for the new dev environment.
            include_services: Whether to copy services.
            selected_services: List of {id, type} dicts for specific services.

        Returns:
            The newly created environment dict.
        """
        params: dict[str, Any] = {
            "environmentId": source_env_id,
            "name": name,
            "duplicateInSameProject": True,
            "includeServices": include_services,
        }
        if selected_services is not None:
            params["selectedServices"] = selected_services
        return self._mutation("environment.duplicate", params)

    def remove_environment(self, env_id: str) -> dict[str, Any]:
        """Remove an environment and all its services."""
        return self._mutation("environment.remove", {"environmentId": env_id})

    # -- Compose --

    def stop_compose(self, compose_id: str) -> dict[str, Any]:
        """Stop a compose stack's containers."""
        return self._mutation("compose.stop", {"composeId": compose_id})

    def start_compose(self, compose_id: str) -> dict[str, Any]:
        """Start a compose stack's containers."""
        return self._mutation("compose.deployCompose", {"composeId": compose_id})

    def update_compose(self, compose_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update compose properties (env, composeFile, etc.)."""
        params = {"composeId": compose_id, **kwargs}
        return self._mutation("compose.update", params)

    # -- Application --

    def stop_application(self, app_id: str) -> dict[str, Any]:
        """Stop an application."""
        return self._mutation("application.stop", {"applicationId": app_id})

    def start_application(self, app_id: str) -> dict[str, Any]:
        """Start/deploy an application."""
        return self._mutation("application.deploy", {"applicationId": app_id})
