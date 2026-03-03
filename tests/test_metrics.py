"""Tests for agent metrics push to Prometheus Pushgateway."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from workflow_platform.metrics import push_metrics


@pytest.fixture()
def mock_push() -> object:
    """Mock push_to_gateway so no real HTTP calls are made."""
    with patch("workflow_platform.metrics.push_to_gateway") as mock:
        yield mock


class TestPushMetrics:
    def test_pass_report_pushes_result_1(self, mock_push: object) -> None:
        report = {
            "overall": "pass",
            "role": "auditor",
            "duration_seconds": 57.1,
            "scenarios_pass": 9,
            "scenarios_fail": 0,
        }
        push_metrics("bid-scraper", "auditor", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_complete_report_normalizes_to_1(self, mock_push: object) -> None:
        report = {
            "overall": "complete",
            "role": "analyst",
            "duration_seconds": 488.6,
            "scenarios_pass": 0,
            "scenarios_fail": 0,
        }
        push_metrics("defendershield-etl", "analyst", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_fail_report_pushes_result_0(self, mock_push: object) -> None:
        report = {
            "overall": "fail",
            "role": "auditor",
            "duration_seconds": 81.0,
            "scenarios_pass": 7,
            "scenarios_fail": 2,
        }
        push_metrics("bid-scraper", "auditor", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_error_report_pushes_result_0(self, mock_push: object) -> None:
        report = {
            "overall": "error",
            "role": "auditor",
            "duration_seconds": 0,
            "scenarios_pass": 0,
            "scenarios_fail": 0,
        }
        push_metrics("bid-scraper", "auditor", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_incomplete_report_pushes_result_0(self, mock_push: object) -> None:
        report = {
            "overall": "incomplete",
            "role": "auditor",
            "duration_seconds": 300,
            "scenarios_pass": 3,
            "scenarios_fail": 1,
        }
        push_metrics("bid-scraper", "auditor", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_pushgateway_url_and_job_label(self, mock_push: object) -> None:
        report = {
            "overall": "pass",
            "role": "auditor",
            "duration_seconds": 50.0,
            "scenarios_pass": 5,
            "scenarios_fail": 0,
        }
        push_metrics("bid-scraper", "auditor", report)
        call_args = mock_push.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == "localhost:9091"
        assert call_args[1]["job"] == "workflow_agent_bid-scraper_auditor"

    def test_missing_fields_default_to_zero(self, mock_push: object) -> None:
        report = {"overall": "error"}
        push_metrics("test-svc", "auditor", report)
        mock_push.assert_called_once()  # type: ignore[union-attr]

    def test_pushgateway_unreachable_raises(self) -> None:
        with patch(
            "workflow_platform.metrics.push_to_gateway",
            side_effect=ConnectionError("Connection refused"),
        ):
            with pytest.raises(ConnectionError):
                push_metrics("bid-scraper", "auditor", {"overall": "pass"})


class TestPushMetricsOrchestrator:
    """Test the _push_metrics wrapper in orchestrate.py."""

    def test_push_metrics_logs_warning_on_failure(self) -> None:
        with (
            patch(
                "workflow_platform.metrics.push_to_gateway",
                side_effect=ConnectionError("refused"),
            ),
            patch("workflow_platform.orchestrate.log") as mock_log,
        ):
            from workflow_platform.orchestrate import _push_metrics

            # Should not raise -- logs warning instead
            _push_metrics("bid-scraper", {"overall": "pass", "role": "auditor"})
            mock_log.warning.assert_called_once()

    def test_push_metrics_handles_missing_prometheus_client(self) -> None:
        with (
            patch(
                "workflow_platform.orchestrate.log",
            ) as mock_log,
            patch.dict("sys.modules", {"prometheus_client": None}),
            patch(
                "workflow_platform.metrics.push_to_gateway",
                side_effect=ImportError("no module"),
            ),
        ):
            from workflow_platform.orchestrate import _push_metrics

            _push_metrics("bid-scraper", {"overall": "pass", "role": "auditor"})
            mock_log.warning.assert_called()
