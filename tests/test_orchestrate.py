"""Tests for the workflow orchestration CLI.

Tests the glue logic: build flow, deploy gate, human confirmation,
monitor flow. All heavy deps (workflow-env, workflow-agent, git) are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from workflow_platform.orchestrate import (
    _check_container_running,
    _confirm,
    _exec_service,
    _find_report_by_run_id,
    _latest_report,
    cmd_build,
    cmd_deploy,
    cmd_monitor,
)

# -- Latest report lookup --


class TestLatestReport:
    def test_finds_most_recent_report(self, tmp_path: Path) -> None:
        import os  # noqa: E401
        import time

        svc_dir = tmp_path / "agent-output" / "test-svc"
        svc_dir.mkdir(parents=True)

        old_dir = svc_dir / "auditor_2026-02-20_060000"
        old_dir.mkdir()
        (old_dir / "report.json").write_text(json.dumps({"overall": "fail"}))
        # Set old mtime so sort-by-mtime picks the newer dir
        os.utime(old_dir, (time.time() - 200, time.time() - 200))

        new_dir = svc_dir / "auditor_2026-02-22_060000"
        new_dir.mkdir()
        (new_dir / "report.json").write_text(json.dumps({"overall": "pass"}))

        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _latest_report("test-svc")

        assert result is not None
        assert result["overall"] == "pass"

    def test_returns_none_when_no_reports(self, tmp_path: Path) -> None:
        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _latest_report("nonexistent-service")
        assert result is None

    def test_role_filter_ignores_other_roles(self, tmp_path: Path) -> None:
        """monthly-analyst dirs must not shadow auditor results."""
        import os  # noqa: E401
        import time

        svc_dir = tmp_path / "agent-output" / "test-svc"
        svc_dir.mkdir(parents=True)

        # Auditor report (older mtime)
        aud_dir = svc_dir / "auditor_2026-03-12_111916"
        aud_dir.mkdir()
        (aud_dir / "report.json").write_text(json.dumps({"overall": "error"}))
        os.utime(aud_dir, (time.time() - 100, time.time() - 100))

        # monthly-analyst report (newer mtime, different role)
        analyst_dir = svc_dir / "monthly-analyst_2026-03-11_194011"
        analyst_dir.mkdir()
        (analyst_dir / "report.json").write_text(json.dumps({"overall": "complete"}))

        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _latest_report("test-svc", role="auditor")

        assert result is not None
        assert result["overall"] == "error"

    def test_run_id_finds_exact_match(self, tmp_path: Path) -> None:
        """run_id lookup finds the exact directory, ignoring others."""
        svc_dir = tmp_path / "agent-output" / "test-svc"
        svc_dir.mkdir(parents=True)

        # Old report without run_id
        old_dir = svc_dir / "auditor_2026-03-12_111916"
        old_dir.mkdir()
        (old_dir / "report.json").write_text(json.dumps({"overall": "pass"}))

        # New report with run_id
        new_dir = svc_dir / "auditor_2026-03-13_111928_abc12345"
        new_dir.mkdir()
        (new_dir / "report.json").write_text(json.dumps({"overall": "error"}))

        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _find_report_by_run_id("test-svc", "abc12345")

        assert result is not None
        assert result["overall"] == "error"

    def test_run_id_returns_none_when_not_found(self, tmp_path: Path) -> None:
        svc_dir = tmp_path / "agent-output" / "test-svc"
        svc_dir.mkdir(parents=True)

        d = svc_dir / "auditor_2026-03-12_111916"
        d.mkdir()
        (d / "report.json").write_text(json.dumps({"overall": "pass"}))

        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _find_report_by_run_id("test-svc", "nonexistent")

        assert result is None


# -- Human confirmation --


class TestConfirm:
    def test_y_returns_true(self) -> None:
        with patch("builtins.input", return_value="y"):
            assert _confirm("Continue?") is True

    def test_n_returns_false(self) -> None:
        with patch("builtins.input", return_value="n"):
            assert _confirm("Continue?") is False

    def test_empty_returns_false(self) -> None:
        with patch("builtins.input", return_value=""):
            assert _confirm("Continue?") is False

    def test_eof_returns_false(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert _confirm("Continue?") is False


# -- Build command --


class TestCmdBuild:
    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.cmd_up")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_build_runs_full_cycle(
        self,
        mock_config: MagicMock,
        mock_get_client: MagicMock,
        mock_cmd_up: MagicMock,
        mock_run_agent: MagicMock,
    ) -> None:
        mock_cmd_up.return_value = {"environmentId": "dev-123", "name": "dev-test"}
        mock_run_agent.return_value = (
            {
                "overall": "pass",
                "scenarios_pass": 3,
                "scenarios_fail": 0,
                "scenarios_error": 0,
                "summary": "All good",
                "scenarios": [],
            },
            "test1234",
        )

        report = cmd_build("test-service", force=True)

        assert report["overall"] == "pass"
        mock_cmd_up.assert_called_once()
        mock_run_agent.assert_called_once_with(
            "test-service",
            "auditor",
            model="sonnet",
            max_turns=50,
            timeout=600,
        )

    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.cmd_up")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_build_returns_failing_report(
        self,
        mock_config: MagicMock,
        mock_get_client: MagicMock,
        mock_cmd_up: MagicMock,
        mock_run_agent: MagicMock,
    ) -> None:
        mock_cmd_up.return_value = {"environmentId": "dev-123"}
        mock_run_agent.return_value = (
            {
                "overall": "fail",
                "scenarios_pass": 1,
                "scenarios_fail": 2,
                "scenarios_error": 0,
                "summary": "Data issues",
                "scenarios": [
                    {"id": 1, "status": "pass", "description": "ok"},
                    {"id": 2, "status": "fail", "description": "stale"},
                ],
            },
            "test1234",
        )

        report = cmd_build("test", force=True)
        assert report["overall"] == "fail"


# -- Deploy command --


class TestCmdDeploy:
    @patch("workflow_platform.orchestrate.cmd_destroy")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate._send_deploy_notification")
    @patch("subprocess.run")
    @patch("workflow_platform.orchestrate._confirm", return_value=True)
    @patch("workflow_platform.orchestrate._latest_report")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_deploy_succeeds_with_passing_audit(
        self,
        mock_config: MagicMock,
        mock_latest: MagicMock,
        mock_confirm: MagicMock,
        mock_subprocess: MagicMock,
        mock_notify: MagicMock,
        mock_get_client: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Create a fake repo dir
        repo = tmp_path / "repo"
        repo.mkdir()

        mock_latest.return_value = {
            "overall": "pass",
            "scenarios_pass": 3,
        }
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ok = cmd_deploy("test-service", str(repo), branch="main")

        assert ok is True
        mock_notify.assert_called_once()

    @patch("workflow_platform.orchestrate._latest_report")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_deploy_blocked_by_failing_audit(
        self,
        mock_config: MagicMock,
        mock_latest: MagicMock,
    ) -> None:
        mock_latest.return_value = {"overall": "fail", "scenarios_pass": 1}

        ok = cmd_deploy("test-service", "/fake/repo")

        assert ok is False

    @patch("workflow_platform.orchestrate._latest_report")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_deploy_blocked_when_no_audit(
        self,
        mock_config: MagicMock,
        mock_latest: MagicMock,
    ) -> None:
        mock_latest.return_value = None

        ok = cmd_deploy("test-service", "/fake/repo")

        assert ok is False

    @patch("workflow_platform.orchestrate._confirm", return_value=False)
    @patch("workflow_platform.orchestrate._latest_report")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_deploy_cancelled_by_human(
        self,
        mock_config: MagicMock,
        mock_latest: MagicMock,
        mock_confirm: MagicMock,
    ) -> None:
        mock_latest.return_value = {"overall": "pass", "scenarios_pass": 3}

        ok = cmd_deploy("test-service", "/fake/repo")

        assert ok is False
        mock_confirm.assert_called()

    @patch("workflow_platform.orchestrate.cmd_destroy")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate._send_deploy_notification")
    @patch("subprocess.run")
    @patch("workflow_platform.orchestrate._confirm", return_value=True)
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_deploy_with_skip_audit_check(
        self,
        mock_config: MagicMock,
        mock_confirm: MagicMock,
        mock_subprocess: MagicMock,
        mock_notify: MagicMock,
        mock_get_client: MagicMock,
        mock_destroy: MagicMock,
        tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ok = cmd_deploy("test", str(repo), skip_audit_check=True)

        assert ok is True


# -- Container helpers --


class TestCheckContainerRunning:
    @patch("workflow_platform.orchestrate.subprocess.run")
    def test_returns_true_when_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
        assert _check_container_running("my-container") is True

    @patch("workflow_platform.orchestrate.subprocess.run")
    def test_returns_false_when_stopped(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="false\n")
        assert _check_container_running("my-container") is False

    @patch("workflow_platform.orchestrate.subprocess.run")
    def test_returns_false_when_not_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _check_container_running("nonexistent") is False


class TestExecService:
    @patch("workflow_platform.orchestrate.subprocess.run")
    def test_returns_exit_code_and_output(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        code, out, err = _exec_service("container", "python -m mymod", service="test")
        assert code == 0
        assert out == "ok\n"
        assert err == ""

    @patch("workflow_platform.orchestrate.subprocess.run")
    def test_splits_command_string(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _exec_service("ctr", "python -m mymod run", service="test")
        call_args = mock_run.call_args[0][0]
        assert call_args == ["docker", "exec", "ctr", "python", "-m", "mymod", "run"]


# -- Monitor command --


class TestCmdMonitor:
    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_monitor_delegates_to_workflow_agent(
        self, mock_config: MagicMock, mock_run_agent: MagicMock
    ) -> None:
        mock_run_agent.return_value = (
            {"overall": "pass", "summary": "All healthy", "scenarios": []},
            "test1234",
        )

        report = cmd_monitor("bid-scraper")

        assert report["overall"] == "pass"
        mock_run_agent.assert_called_once_with(
            "bid-scraper",
            "auditor",
            model="sonnet",
            max_turns=50,
            timeout=600,
        )

    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_monitor_returns_failures(
        self, mock_config: MagicMock, mock_run_agent: MagicMock
    ) -> None:
        mock_run_agent.return_value = (
            {
                "overall": "fail",
                "summary": "Stale data detected",
                "scenarios": [{"id": 1, "status": "fail"}],
            },
            "test1234",
        )

        report = cmd_monitor("bid-scraper")

        assert report["overall"] == "fail"

    @patch("workflow_platform.orchestrate._find_report_dir_by_run_id")
    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate._exec_service")
    @patch("workflow_platform.orchestrate._check_container_running", return_value=True)
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_exec_runs_before_audit(
        self,
        mock_config: MagicMock,
        mock_check: MagicMock,
        mock_exec: MagicMock,
        mock_run_agent: MagicMock,
        mock_report_dir: MagicMock,
        tmp_path: Path,
    ) -> None:
        archive = tmp_path / "archive"
        archive.mkdir()
        mock_report_dir.return_value = archive

        mock_config.return_value.service_containers = {"etl": "etl-container"}
        mock_exec.return_value = (0, "ETL done\n", "")
        mock_run_agent.return_value = (
            {"overall": "pass", "summary": "ok", "scenarios": []},
            "test1234",
        )

        report = cmd_monitor("etl", exec_command="python -m etl run")

        assert report["overall"] == "pass"
        mock_check.assert_called_once_with("etl-container")
        mock_exec.assert_called_once()
        mock_run_agent.assert_called_once()
        # Verify exec output was saved alongside the report
        assert (archive / "exec_output.log").exists()

    @patch("workflow_platform.orchestrate._find_report_dir_by_run_id")
    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate._notify_exec_failure")
    @patch("workflow_platform.orchestrate._exec_service")
    @patch("workflow_platform.orchestrate._check_container_running", return_value=True)
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_exec_failure_still_audits(
        self,
        mock_config: MagicMock,
        mock_check: MagicMock,
        mock_exec: MagicMock,
        mock_notify_exec: MagicMock,
        mock_run_agent: MagicMock,
        mock_report_dir: MagicMock,
        tmp_path: Path,
    ) -> None:
        archive = tmp_path / "archive"
        archive.mkdir()
        mock_report_dir.return_value = archive

        mock_config.return_value.service_containers = {"etl": "etl-ctr"}
        mock_exec.return_value = (1, "", "API timeout\n")
        mock_run_agent.return_value = (
            {"overall": "pass", "summary": "data ok", "scenarios": []},
            "test1234",
        )

        report = cmd_monitor("etl", exec_command="python -m etl run")

        assert report["overall"] == "pass"
        mock_notify_exec.assert_called_once_with("etl", 1, "API timeout\n")
        mock_run_agent.assert_called_once()

    @patch("workflow_platform.orchestrate._notify_container_not_running")
    @patch("workflow_platform.orchestrate._check_container_running", return_value=False)
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_container_not_running_exits(
        self,
        mock_config: MagicMock,
        mock_check: MagicMock,
        mock_notify_ctr: MagicMock,
    ) -> None:
        mock_config.return_value.service_containers = {"etl": "etl-ctr"}

        import pytest

        with pytest.raises(SystemExit):
            cmd_monitor("etl", exec_command="python -m etl run")

        mock_notify_ctr.assert_called_once_with("etl", "etl-ctr")

    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_monitor_without_exec(self, mock_config: MagicMock, mock_run_agent: MagicMock) -> None:
        """When --exec is not provided, monitor is audit-only (no docker exec)."""
        mock_run_agent.return_value = (
            {"overall": "pass", "summary": "ok", "scenarios": []},
            "test1234",
        )

        report = cmd_monitor("bid-scraper")

        assert report["overall"] == "pass"
        mock_run_agent.assert_called_once()

    @patch("workflow_platform.orchestrate._run_workflow_agent")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_audit_timeout_passed_through(
        self, mock_config: MagicMock, mock_run_agent: MagicMock
    ) -> None:
        mock_run_agent.return_value = (
            {"overall": "pass", "summary": "ok", "scenarios": []},
            "test1234",
        )

        cmd_monitor("etl", audit_timeout=300)

        call_kwargs = mock_run_agent.call_args.kwargs
        assert call_kwargs["timeout"] == 300
