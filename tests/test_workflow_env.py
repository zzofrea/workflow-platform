"""Tests for the workflow-env CLI logic.

Maps to Phase 3 acceptance specs 1-6.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workflow_platform.config import PlatformConfig
from workflow_platform.dokploy_client import DokployClient
from workflow_platform.workflow_env import (
    _find_dev_env,
    _find_service_in_env,
    _rewrite_compose_for_dev,
    check_resources,
    cmd_destroy,
    cmd_list,
    cmd_up,
)

PROD_ENV_ID = "prod-env-123"
PROJECT_ID = "proj-abc"

SAMPLE_PROJECT = {
    "projectId": PROJECT_ID,
    "environments": [
        {
            "environmentId": PROD_ENV_ID,
            "name": "production",
            "isDefault": True,
            "compose": [
                {
                    "composeId": "comp-bid",
                    "name": "bid-scraper",
                    "composeFile": (
                        "services:\n"
                        "  postgres:\n"
                        "    hostname: bid-scraper-postgres\n"
                        "    environment:\n"
                        "      - DATABASE_URL=postgresql://x:x@bid-scraper-postgres:5432/db\n"
                        "  scraper:\n"
                        "    image: bid-scraper:latest\n"
                        "volumes:\n"
                        "  bid_pgdata:\n"
                        "    external: true\n"
                        "    name: prod_bid_pgdata\n"
                    ),
                },
            ],
            "applications": [],
            "mariadb": [],
            "mongo": [],
            "mysql": [],
            "postgres": [],
            "redis": [],
        }
    ],
}


@pytest.fixture()
def config() -> PlatformConfig:
    return PlatformConfig(
        dokploy_url="http://localhost:3000",
        dokploy_api_key="test-key",
        dokploy_project_id=PROJECT_ID,
        dokploy_prod_env_id=PROD_ENV_ID,
        max_containers=18,
        min_free_ram_mb=3072,
    )


# -- Helper tests --


class TestFindService:
    def test_finds_compose(self) -> None:
        env = SAMPLE_PROJECT["environments"][0]
        result = _find_service_in_env(env, "bid-scraper")
        assert result == ("comp-bid", "compose")

    def test_returns_none_for_missing(self) -> None:
        env = SAMPLE_PROJECT["environments"][0]
        assert _find_service_in_env(env, "nonexistent") is None


class TestFindDevEnv:
    def test_finds_existing_dev(self) -> None:
        project = {
            "environments": [
                {"environmentId": "dev-123", "name": "dev-bid-scraper"},
            ]
        }
        result = _find_dev_env(project, "bid-scraper")
        assert result is not None
        assert result["environmentId"] == "dev-123"

    def test_returns_none_when_no_dev(self) -> None:
        assert _find_dev_env(SAMPLE_PROJECT, "bid-scraper") is None


# -- Spec 1: Spin up dev environment --


class TestCmdUp:
    """Spec 1: Dev environment creation from prod."""

    @patch("workflow_platform.workflow_env.check_resources", return_value=[])
    def test_creates_dev_environment(
        self, mock_resources: MagicMock, config: PlatformConfig
    ) -> None:
        client = MagicMock(spec=DokployClient)

        # First call: get project (no dev env yet)
        # Second call: get project (to find services in new env for overrides)
        client.get_project.return_value = SAMPLE_PROJECT

        client.duplicate_environment.return_value = {
            "environmentId": "dev-new-123",
            "name": "dev-bid-scraper",
        }

        result = cmd_up(client, config, "bid-scraper", force=True)

        assert result["environmentId"] == "dev-new-123"
        client.duplicate_environment.assert_called_once()
        call_kwargs = client.duplicate_environment.call_args.kwargs
        assert call_kwargs["name"] == "dev-bid-scraper"
        assert call_kwargs["selected_services"] == [{"id": "comp-bid", "type": "compose"}]

    def test_skips_if_dev_already_exists(self, config: PlatformConfig) -> None:
        project_with_dev = {
            "projectId": PROJECT_ID,
            "environments": [
                SAMPLE_PROJECT["environments"][0],
                {"environmentId": "existing-dev", "name": "dev-bid-scraper"},
            ],
        }
        client = MagicMock(spec=DokployClient)
        client.get_project.return_value = project_with_dev

        result = cmd_up(client, config, "bid-scraper", force=True)
        assert result["environmentId"] == "existing-dev"
        client.duplicate_environment.assert_not_called()


# -- Spec 3: Destroy dev environment --


class TestCmdDestroy:
    def test_removes_dev_environment(self, config: PlatformConfig) -> None:
        project_with_dev = {
            "projectId": PROJECT_ID,
            "environments": [
                SAMPLE_PROJECT["environments"][0],
                {
                    "environmentId": "dev-to-remove",
                    "name": "dev-bid-scraper",
                    "compose": [],
                    "applications": [],
                },
            ],
        }
        client = MagicMock(spec=DokployClient)
        client.get_project.return_value = project_with_dev
        client.remove_environment.return_value = {"environmentId": "dev-to-remove"}

        cmd_destroy(client, config, "bid-scraper")
        client.remove_environment.assert_called_once_with("dev-to-remove")


# -- Spec 4: List dev environments --


class TestCmdList:
    def test_lists_dev_envs(self, config: PlatformConfig) -> None:
        project_with_devs = {
            "projectId": PROJECT_ID,
            "environments": [
                SAMPLE_PROJECT["environments"][0],
                {
                    "environmentId": "dev-1",
                    "name": "dev-bid-scraper",
                    "createdAt": "2026-02-22T00:00:00Z",
                    "compose": [{"composeId": "c1", "name": "bid-scraper"}],
                    "applications": [],
                },
            ],
        }
        client = MagicMock(spec=DokployClient)
        client.get_project.return_value = project_with_devs

        results = cmd_list(client, config)
        assert len(results) == 1
        assert results[0]["service"] == "bid-scraper"

    def test_empty_when_no_devs(self, config: PlatformConfig) -> None:
        client = MagicMock(spec=DokployClient)
        client.get_project.return_value = SAMPLE_PROJECT

        results = cmd_list(client, config)
        assert len(results) == 0


# -- Spec 5: Resource guard --


class TestResourceGuard:
    @patch("subprocess.run")
    def test_warns_on_high_container_count(
        self, mock_run: MagicMock, config: PlatformConfig
    ) -> None:
        # Simulate 20 containers
        mock_run.return_value = MagicMock(
            stdout="\n".join([f"container-{i}" for i in range(20)]),
            returncode=0,
        )
        config.max_containers = 18
        warnings = check_resources(config)
        assert any("Container count" in w for w in warnings)

    @patch("subprocess.run")
    def test_no_warning_when_under_limit(self, mock_run: MagicMock, config: PlatformConfig) -> None:
        mock_run.return_value = MagicMock(
            stdout="\n".join([f"c-{i}" for i in range(10)]),
            returncode=0,
        )
        config.max_containers = 18
        # Only check container count (skip RAM check which needs 'free')
        warnings = [w for w in check_resources(config) if "Container" in w]
        assert len(warnings) == 0


# -- Spec 6: Dev-specific compose rewrite --


class TestComposeRewrite:
    def test_rewrites_hostname(self) -> None:
        original = "    hostname: bid-scraper-postgres\n"
        result = _rewrite_compose_for_dev(original, "bid-scraper")
        assert "bid-scraper-dev-postgres" in result

    def test_rewrites_db_url(self) -> None:
        original = "      - DATABASE_URL=postgresql://x:x@bid-scraper-postgres:5432/db\n"
        result = _rewrite_compose_for_dev(original, "bid-scraper")
        assert "bid-scraper-dev-postgres" in result

    def test_rewrites_db_host_env(self) -> None:
        original = "      - DB_HOST=ds-etl-postgres\n"
        result = _rewrite_compose_for_dev(original, "ds-etl")
        assert "ds-etl-dev-postgres" in result

    def test_removes_external_volume(self) -> None:
        original = "volumes:\n  bid_pgdata:\n    external: true\n    name: prod_bid_pgdata\n"
        result = _rewrite_compose_for_dev(original, "bid-scraper")
        assert "external: true" not in result
        assert "dev: using local volume" in result
