"""Tests for the Dokploy tRPC API client."""

from __future__ import annotations

import httpx
import pytest
import respx

from workflow_platform.dokploy_client import DokployClient, DokployError

BASE = "http://localhost:3000"
KEY = "test-api-key"


@pytest.fixture()
def client() -> DokployClient:
    return DokployClient(BASE, KEY)


class TestQuery:
    """tRPC query (GET) handling."""

    @respx.mock
    def test_successful_query(self, client: DokployClient) -> None:
        respx.get(url__startswith=f"{BASE}/api/trpc/project.one").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"data": {"json": {"projectId": "abc", "name": "test"}}}},
            )
        )
        result = client.get_project("abc")
        assert result["projectId"] == "abc"

    @respx.mock
    def test_error_response_raises(self, client: DokployClient) -> None:
        respx.get(url__startswith=f"{BASE}/api/trpc/project.one").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "json": {
                            "message": "Project not found",
                            "data": {"httpStatus": 404},
                        }
                    }
                },
            )
        )
        with pytest.raises(DokployError, match="Project not found"):
            client.get_project("nonexistent")


class TestMutation:
    """tRPC mutation (POST) handling."""

    @respx.mock
    def test_duplicate_environment(self, client: DokployClient) -> None:
        respx.post(f"{BASE}/api/trpc/environment.duplicate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "data": {
                            "json": {
                                "environmentId": "new-123",
                                "name": "dev-bid-scraper",
                            }
                        }
                    }
                },
            )
        )
        result = client.duplicate_environment(
            source_env_id="prod-env-id",
            name="dev-bid-scraper",
            selected_services=[{"id": "svc-1", "type": "compose"}],
        )
        assert result["environmentId"] == "new-123"

    @respx.mock
    def test_remove_environment(self, client: DokployClient) -> None:
        respx.post(f"{BASE}/api/trpc/environment.remove").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"data": {"json": {"environmentId": "env-123"}}}},
            )
        )
        result = client.remove_environment("env-123")
        assert result["environmentId"] == "env-123"

    @respx.mock
    def test_stop_compose(self, client: DokployClient) -> None:
        respx.post(f"{BASE}/api/trpc/compose.stop").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"data": {"json": True}}},
            )
        )
        # Should not raise
        client.stop_compose("compose-123")
