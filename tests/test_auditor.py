"""Tests for the host-side auditor wrapper.

Tests input preparation, docker command construction, notification routing,
and severity classification. Does NOT run actual containers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from workflow_platform.auditor import (
    _classify_severity,
    build_docker_cmd,
    prepare_input,
    route_notifications,
)

# -- Input preparation --


class TestPrepareInput:
    def test_copies_files_to_input_dir(self, tmp_path: Path) -> None:
        # Create source files
        spec = tmp_path / "my_spec.md"
        spec.write_text("GIVEN x.\nWHEN y.\nTHEN z.")
        access = tmp_path / "my_access.md"
        access.write_text("psql -h db -U user")

        input_dir = tmp_path / "input"
        input_dir.mkdir()

        prepare_input(str(input_dir), str(spec), str(access))

        assert (input_dir / "spec.md").exists()
        assert (input_dir / "access.md").exists()
        assert (input_dir / "spec.md").read_text() == "GIVEN x.\nWHEN y.\nTHEN z."
        assert (input_dir / "access.md").read_text() == "psql -h db -U user"


# -- Docker command construction --


class TestBuildDockerCmd:
    def test_basic_command_structure(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="bid-scraper",
            mode="build",
            model="sonnet",
        )
        assert cmd[0:2] == ["docker", "run"]
        assert "--rm" in cmd
        assert "workflow-auditor:latest" in cmd

    def test_mounts_claude_auth_to_staging(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="bid-scraper",
        )
        # Find the -v flags for claude auth (json + dir)
        mount_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-v"]
        claude_mounts = [m for m in mount_pairs if ".claude" in m]
        assert len(claude_mounts) == 2
        # Both should mount to /audit/auth/ staging dir, read-only
        for m in claude_mounts:
            assert "/audit/auth/" in m
            assert ":ro" in m

    def test_mounts_input_readonly(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
        )
        mount_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-v"]
        input_mount = [m for m in mount_pairs if "/audit/input" in m]
        assert len(input_mount) == 1
        assert ":ro" in input_mount[0]

    def test_mounts_output_readwrite(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
        )
        mount_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-v"]
        output_mount = [m for m in mount_pairs if "/audit/output" in m]
        assert len(output_mount) == 1
        assert ":rw" in output_mount[0]

    def test_joins_network(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
            network="dokploy-network",
        )
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "dokploy-network"

    def test_sets_environment_vars(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="bid-scraper",
            mode="prod",
            model="opus",
            max_turns=10,
        )
        env_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert "AUDITOR_MODE=prod" in env_pairs
        assert "AUDITOR_MODEL=opus" in env_pairs
        assert "AUDITOR_SERVICE=bid-scraper" in env_pairs
        assert "AUDITOR_MAX_TURNS=10" in env_pairs

    def test_container_name_includes_service_and_mode(self) -> None:
        cmd = build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="etl",
            mode="prod",
        )
        name_idx = cmd.index("--name")
        assert cmd[name_idx + 1] == "auditor-etl-prod"


# -- Severity classification --


class TestClassifySeverity:
    def test_critical_on_service_down(self) -> None:
        failures = [{"observation": "Service unreachable", "evidence": "connection refused"}]
        assert _classify_severity(failures) == "critical"

    def test_critical_on_no_records(self) -> None:
        failures = [{"observation": "Query returned 0 rows", "evidence": "count=0"}]
        assert _classify_severity(failures) == "critical"

    def test_warning_on_data_quality(self) -> None:
        failures = [{"observation": "30% of rows have null field_x", "evidence": "count=150"}]
        assert _classify_severity(failures) == "warning"

    def test_warning_when_no_keywords(self) -> None:
        failures = [{"observation": "Unexpected format", "evidence": "field was int not str"}]
        assert _classify_severity(failures) == "warning"


# -- Notification routing --


class TestRouteNotifications:
    @patch("workflow_platform.auditor.fanout")
    @patch("workflow_platform.auditor.NotifyConfig")
    def test_pass_sends_success(self, mock_config_cls: MagicMock, mock_fanout: MagicMock) -> None:
        report = {
            "service": "bid-scraper",
            "overall": "pass",
            "summary": "All scenarios verified",
            "scenarios": [],
        }
        route_notifications(report)
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] == "success"
        assert "PASSED" in call_kwargs["message"]

    @patch("workflow_platform.auditor.fanout")
    @patch("workflow_platform.auditor.NotifyConfig")
    def test_fail_sends_warning_or_critical(
        self, mock_config_cls: MagicMock, mock_fanout: MagicMock
    ) -> None:
        report = {
            "service": "bid-scraper",
            "overall": "fail",
            "summary": "Stale data",
            "scenarios": [
                {"id": 1, "status": "fail", "observation": "null fields", "evidence": "30%"},
            ],
        }
        route_notifications(report)
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] in ("warning", "critical")
        assert "FAILED" in call_kwargs["message"]

    @patch("workflow_platform.auditor.fanout")
    @patch("workflow_platform.auditor.NotifyConfig")
    def test_incomplete_sends_warning(
        self, mock_config_cls: MagicMock, mock_fanout: MagicMock
    ) -> None:
        report = {
            "service": "test",
            "overall": "incomplete",
            "summary": "Token limit",
            "incomplete_reason": "token limit reached",
            "scenarios": [],
        }
        route_notifications(report)
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] == "warning"
        assert "INCOMPLETE" in call_kwargs["message"]

    @patch("workflow_platform.auditor.fanout", None)
    @patch("workflow_platform.auditor.NotifyConfig", None)
    def test_graceful_when_notify_unavailable(self) -> None:
        """When workflow-notify is not installed, route_notifications is a no-op."""
        report = {"service": "x", "overall": "pass", "summary": "ok", "scenarios": []}
        # Should not raise
        route_notifications(report)


# -- Timeout handling --


class TestAuditTimeout:
    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    def test_timeout_produces_error_report(
        self, mock_run: MagicMock, mock_notify: MagicMock, tmp_path: Path
    ) -> None:
        """When the auditor container times out, an error report is produced."""
        import subprocess as sp

        from workflow_platform.auditor import run_audit

        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x. WHEN y. THEN z.")
        access = tmp_path / "access.md"
        access.write_text("psql -h db")

        # First call (docker run) raises TimeoutExpired, subsequent calls (kill, rm) succeed
        mock_run.side_effect = [
            sp.TimeoutExpired(cmd=["docker", "run"], timeout=10),
            MagicMock(returncode=0),  # docker kill
            MagicMock(returncode=0),  # docker rm
        ]

        report = run_audit(
            str(spec),
            str(access),
            service="test-svc",
            mode="prod",
            audit_timeout=10,
            notify=True,
        )

        assert report["overall"] == "error"
        assert "timed out" in report["summary"].lower()
        mock_notify.assert_called_once()
