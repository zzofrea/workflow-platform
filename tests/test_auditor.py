"""Tests for the host-side auditor wrapper.

Tests input preparation, docker command construction, notification routing,
and severity classification. Does NOT run actual containers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from workflow_platform.auditor import (
    _classify_severity,
    _image_exists_locally,
    prepare_input,
    pull_image,
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


# -- Image pull logic --


class TestPullImage:
    @patch("workflow_platform.auditor.subprocess.run")
    def test_pull_succeeds(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert pull_image() is True
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "pull", "ghcr.io/zzofrea/workflow-auditor:latest"]

    @patch("workflow_platform.auditor.subprocess.run")
    def test_pull_fails(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        assert pull_image() is False

    @patch("workflow_platform.auditor.subprocess.run")
    def test_image_exists_locally(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert _image_exists_locally() is True

    @patch("workflow_platform.auditor.subprocess.run")
    def test_image_not_exists_locally(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        assert _image_exists_locally() is False


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


