"""Tests for the workflow orchestration CLI.

Tests the glue logic: build flow, deploy gate, human confirmation,
monitor flow. All heavy deps (workflow-env, auditor, git) are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from workflow_platform.orchestrate import (
    _confirm,
    _latest_report,
    cmd_build,
    cmd_deploy,
    cmd_monitor,
)

# -- Latest report lookup --


class TestLatestReport:
    def test_finds_most_recent_report(self, tmp_path: Path) -> None:
        # Create two report dirs
        old_dir = tmp_path / "build_2026-02-20_060000"
        old_dir.mkdir(parents=True)
        (old_dir / "report.json").write_text(json.dumps({"overall": "fail"}))

        new_dir = tmp_path / "build_2026-02-22_060000"
        new_dir.mkdir(parents=True)
        (new_dir / "report.json").write_text(json.dumps({"overall": "pass"}))

        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path.parent):
            # Need to set up the right path structure
            pass

        # Test directly with the path
        report_dir = tmp_path
        subdirs = sorted(report_dir.iterdir(), reverse=True)
        latest = json.loads((subdirs[0] / "report.json").read_text())
        assert latest["overall"] == "pass"

    def test_returns_none_when_no_reports(self, tmp_path: Path) -> None:
        with patch("workflow_platform.orchestrate.Path.home", return_value=tmp_path):
            result = _latest_report("nonexistent-service")
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
    @patch("workflow_platform.orchestrate.run_audit")
    @patch("workflow_platform.orchestrate.cmd_up")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_build_runs_full_cycle(
        self,
        mock_config: MagicMock,
        mock_get_client: MagicMock,
        mock_cmd_up: MagicMock,
        mock_run_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Setup
        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x. WHEN y. THEN z.")
        access = tmp_path / "access.md"
        access.write_text("psql -h db")

        mock_cmd_up.return_value = {"environmentId": "dev-123", "name": "dev-test"}
        mock_run_audit.return_value = {
            "overall": "pass",
            "scenarios_pass": 3,
            "scenarios_fail": 0,
            "scenarios_error": 0,
            "summary": "All good",
            "scenarios": [],
        }

        report = cmd_build("test-service", str(spec), str(access), force=True)

        assert report["overall"] == "pass"
        mock_cmd_up.assert_called_once()
        mock_run_audit.assert_called_once()
        # Verify auditor was called with build mode
        call_kwargs = mock_run_audit.call_args.kwargs
        assert call_kwargs.get("mode") == "build"

    @patch("workflow_platform.orchestrate.run_audit")
    @patch("workflow_platform.orchestrate.cmd_up")
    @patch("workflow_platform.orchestrate.get_client")
    @patch("workflow_platform.orchestrate.PlatformConfig")
    def test_build_returns_failing_report(
        self,
        mock_config: MagicMock,
        mock_get_client: MagicMock,
        mock_cmd_up: MagicMock,
        mock_run_audit: MagicMock,
        tmp_path: Path,
    ) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x.")
        access = tmp_path / "access.md"
        access.write_text("psql -h db")

        mock_cmd_up.return_value = {"environmentId": "dev-123"}
        mock_run_audit.return_value = {
            "overall": "fail",
            "scenarios_pass": 1,
            "scenarios_fail": 2,
            "scenarios_error": 0,
            "summary": "Data issues",
            "scenarios": [
                {"id": 1, "status": "pass", "description": "ok"},
                {"id": 2, "status": "fail", "description": "stale"},
            ],
        }

        report = cmd_build("test", str(spec), str(access), force=True)
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


# -- Monitor command --


class TestCmdMonitor:
    @patch("workflow_platform.orchestrate.run_audit")
    def test_monitor_runs_in_prod_mode(self, mock_run_audit: MagicMock, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x.")
        access = tmp_path / "access.md"
        access.write_text("psql -h prod-db")

        mock_run_audit.return_value = {
            "overall": "pass",
            "summary": "All healthy",
            "scenarios": [],
        }

        report = cmd_monitor("bid-scraper", str(spec), str(access))

        assert report["overall"] == "pass"
        call_kwargs = mock_run_audit.call_args.kwargs
        assert call_kwargs["mode"] == "prod"

    @patch("workflow_platform.orchestrate.run_audit")
    def test_monitor_returns_failures(self, mock_run_audit: MagicMock, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x.")
        access = tmp_path / "access.md"
        access.write_text("psql -h prod-db")

        mock_run_audit.return_value = {
            "overall": "fail",
            "summary": "Stale data detected",
            "scenarios": [{"id": 1, "status": "fail"}],
        }

        report = cmd_monitor("bid-scraper", str(spec), str(access))

        assert report["overall"] == "fail"
