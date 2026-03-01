"""Tests for infrastructure health checks and boot notifications.

All system calls (docker, df, free) and workflow-notify are mocked.
Tests verify that correct severity is chosen and findings are reported.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workflow_platform.health import (
    BOOT_DELAY_SECONDS,
    EXPECTED_CONTAINERS,
    _find_container_status,
    _get_container_statuses,
    _get_disk_usage,
    _get_memory_usage,
    _is_docker_available,
    cmd_boot,
    cmd_check,
)

# -- Unit tests for metric collectors --


class TestGetDiskUsage:
    def test_parses_df_output(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Mounted on  Use%\n/           42%\n/home       67%\n"
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            mounts = _get_disk_usage()
        assert len(mounts) == 2
        assert mounts[0] == {"mount": "/", "percent": 42.0}
        assert mounts[1] == {"mount": "/home", "percent": 67.0}

    def test_returns_empty_on_failure(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            mounts = _get_disk_usage()
        assert mounts == []


class TestGetMemoryUsage:
    def test_parses_free_output(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "              total        used        free\n"
            "Mem:          16000        8000        8000\n"
            "Swap:          4000           0        4000\n"
        )
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            pct = _get_memory_usage()
        assert pct == 50.0

    def test_returns_negative_on_failure(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            pct = _get_memory_usage()
        assert pct == -1.0


class TestGetContainerStatuses:
    def test_parses_docker_ps(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "dokploy\tUp 3 weeks\ndokploy-redis\tUp 3 weeks\n"
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            statuses = _get_container_statuses()
        assert statuses == {"dokploy": "Up 3 weeks", "dokploy-redis": "Up 3 weeks"}

    def test_returns_empty_on_failure(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            statuses = _get_container_statuses()
        assert statuses == {}


class TestIsDockerAvailable:
    def test_true_when_daemon_reachable(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            assert _is_docker_available() is True

    def test_false_when_daemon_unreachable(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("workflow_platform.health.subprocess.run", return_value=mock_result):
            assert _is_docker_available() is False


class TestFindContainerStatus:
    def test_exact_match(self) -> None:
        statuses = {"dokploy": "Up 3 weeks", "dokploy-redis": "Up 3 weeks"}
        assert _find_container_status("dokploy", statuses) == "Up 3 weeks"

    def test_prefix_match_with_swarm_suffix(self) -> None:
        statuses = {"dokploy.1.k8u2c7n14id8z753sq326dhvh": "Up 3 weeks"}
        assert _find_container_status("dokploy", statuses) == "Up 3 weeks"

    def test_substring_match_with_compose_prefix(self) -> None:
        statuses = {"compose-parse-back-end-bus-tqs6jx-crowdsec-1": "Up 2 days"}
        assert _find_container_status("crowdsec", statuses) == "Up 2 days"

    def test_no_match_returns_empty(self) -> None:
        statuses = {"dokploy": "Up 3 weeks"}
        assert _find_container_status("nonexistent", statuses) == ""

    def test_exact_match_preferred_over_prefix(self) -> None:
        statuses = {
            "n8n": "Up 1 day",
            "n8n-postgres": "Up 1 day",
        }
        assert _find_container_status("n8n", statuses) == "Up 1 day"


class TestCmdCheckSwarmSuffixes:
    """GIVEN Docker Swarm appends suffixes to container names.
    WHEN the health check runs.
    THEN all containers still match via prefix and report success.
    """

    def test_swarm_suffixed_containers_match(self) -> None:
        statuses = {f"{name}.1.abc123def456": "Up 3 weeks" for name in EXPECTED_CONTAINERS}
        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[{"mount": "/", "percent": 42.0}],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=50.0),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=statuses,
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        severity, _message = mock_notify.call_args[0]
        assert severity == "success"


# -- Acceptance tests for cmd_check --


def _make_all_containers_up() -> dict[str, str]:
    """Return a container status dict where all expected containers are Up."""
    return {name: "Up 3 weeks" for name in EXPECTED_CONTAINERS}


class TestCmdCheckHealthy:
    """GIVEN the system has been running normally.
    WHEN the weekly health check runs.
    THEN a single notification appears with severity 'success'.
    """

    def test_all_healthy_sends_success(self) -> None:
        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[{"mount": "/", "percent": 42.0}],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=61.0),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=_make_all_containers_up(),
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        mock_notify.assert_called_once()
        severity, message = mock_notify.call_args[0]
        assert severity == "success"
        assert "healthy" in message.lower() or "Infrastructure healthy" in message
        assert "42%" in message
        assert "61.0%" in message


class TestCmdCheckDiskWarning:
    """GIVEN one mount point exceeds 85% disk usage.
    WHEN the weekly health check runs.
    THEN a notification with severity 'warning' identifies the mount.
    """

    def test_disk_over_threshold_sends_warning(self) -> None:
        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[
                    {"mount": "/", "percent": 42.0},
                    {"mount": "/data", "percent": 87.0},
                ],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=50.0),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=_make_all_containers_up(),
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        severity, message = mock_notify.call_args[0]
        assert severity == "warning"
        assert "/data" in message
        assert "87%" in message


class TestCmdCheckMemoryWarning:
    """GIVEN memory usage exceeds 90%.
    WHEN the weekly health check runs.
    THEN a notification with severity 'warning' reports memory usage.
    """

    def test_memory_over_threshold_sends_warning(self) -> None:
        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[{"mount": "/", "percent": 42.0}],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=93.5),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=_make_all_containers_up(),
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        severity, message = mock_notify.call_args[0]
        assert severity == "warning"
        assert "93.5%" in message


class TestCmdCheckMissingContainers:
    """GIVEN one or more expected containers are not running.
    WHEN the weekly health check runs.
    THEN a notification with severity 'warning' lists the missing containers.
    """

    def test_missing_container_sends_warning(self) -> None:
        statuses = _make_all_containers_up()
        statuses["n8n"] = "Exited (1) 2 hours ago"
        del statuses["crowdsec"]

        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[{"mount": "/", "percent": 42.0}],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=50.0),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=statuses,
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        severity, message = mock_notify.call_args[0]
        assert severity == "warning"
        assert "n8n" in message
        assert "crowdsec" in message


class TestCmdCheckMultipleFindings:
    """GIVEN disk, memory, AND container issues exist simultaneously.
    WHEN the weekly health check runs.
    THEN a single notification with severity 'warning' lists all findings.
    """

    def test_multiple_issues_single_notification(self) -> None:
        statuses = _make_all_containers_up()
        statuses["n8n"] = "Exited (1) 2 hours ago"

        with (
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_disk_usage",
                return_value=[{"mount": "/", "percent": 88.0}],
            ),
            patch("workflow_platform.health._get_memory_usage", return_value=92.0),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=statuses,
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_check()

        mock_notify.assert_called_once()
        severity, message = mock_notify.call_args[0]
        assert severity == "warning"
        assert "88%" in message
        assert "92.0%" in message
        assert "n8n" in message


class TestCmdCheckDockerDown:
    """GIVEN the Docker daemon is not responding.
    WHEN the weekly health check runs.
    THEN a notification fires with severity 'critical'.
    """

    def test_docker_unavailable_sends_critical(self) -> None:
        with (
            patch("workflow_platform.health._is_docker_available", return_value=False),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            with pytest.raises(SystemExit):
                cmd_check()

        severity, message = mock_notify.call_args[0]
        assert severity == "critical"
        assert "Docker" in message


# -- Acceptance tests for cmd_boot --


class TestCmdBootAllUp:
    """GIVEN all expected containers start successfully after reboot.
    WHEN 5 minutes have elapsed since boot.
    THEN a notification appears with severity 'success'.
    """

    def test_all_containers_up_sends_success(self) -> None:
        with (
            patch("workflow_platform.health.time.sleep"),
            patch(
                "workflow_platform.health.time.monotonic",
                side_effect=[0, 0, BOOT_DELAY_SECONDS + 1],
            ),
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=_make_all_containers_up(),
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_boot()

        severity, message = mock_notify.call_args[0]
        assert severity == "success"
        assert "boot complete" in message.lower()


class TestCmdBootMissingContainers:
    """GIVEN some expected containers failed to start after reboot.
    WHEN 5 minutes have elapsed since boot.
    THEN a notification appears with severity 'warning' listing the missing containers.
    """

    def test_missing_containers_sends_warning(self) -> None:
        statuses = _make_all_containers_up()
        del statuses["n8n"]
        del statuses["n8n-postgres"]

        with (
            patch("workflow_platform.health.time.sleep"),
            patch(
                "workflow_platform.health.time.monotonic",
                side_effect=[0, 0, BOOT_DELAY_SECONDS + 1],
            ),
            patch("workflow_platform.health._is_docker_available", return_value=True),
            patch(
                "workflow_platform.health._get_container_statuses",
                return_value=statuses,
            ),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            cmd_boot()

        severity, message = mock_notify.call_args[0]
        assert severity == "warning"
        assert "n8n" in message
        assert "n8n-postgres" in message


class TestCmdBootDockerUnavailable:
    """GIVEN Docker daemon never becomes available during boot delay.
    WHEN the boot notification runs.
    THEN a critical notification fires.
    """

    def test_docker_never_available_sends_critical(self) -> None:
        with (
            patch("workflow_platform.health.time.sleep"),
            patch(
                "workflow_platform.health.time.monotonic",
                side_effect=[0, 0, BOOT_DELAY_SECONDS + 1],
            ),
            patch("workflow_platform.health._is_docker_available", return_value=False),
            patch("workflow_platform.health._notify") as mock_notify,
        ):
            with pytest.raises(SystemExit):
                cmd_boot()

        severity, message = mock_notify.call_args[0]
        assert severity == "critical"
