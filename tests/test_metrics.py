"""Tests for agent metrics push to Prometheus Pushgateway."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from workflow_platform.metrics import push_briefing_post, push_metrics


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


class TestPushBriefingPost:
    def test_pushes_unix_ts_with_mode_label(self, mock_push: object) -> None:
        push_briefing_post("morning", post_ts=1700000000.0)
        call_args = mock_push.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == "localhost:9091"
        assert call_args[1]["job"] == "daily_briefing_morning"
        # Registry has the gauge with the right label and value
        registry = call_args[1]["registry"]
        # collect() yields one Metric for the family; verify the label+value
        families = list(registry.collect())
        assert any(f.name == "daily_briefing_last_post_ts" for f in families)
        family = next(f for f in families if f.name == "daily_briefing_last_post_ts")
        sample = family.samples[0]
        assert sample.labels == {"mode": "morning"}
        assert sample.value == 1700000000.0

    def test_defaults_to_now_when_ts_omitted(self, mock_push: object) -> None:
        with patch("workflow_platform.metrics.time.time", return_value=1234567890.0):
            push_briefing_post("weekly")
        call_args = mock_push.call_args  # type: ignore[union-attr]
        registry = call_args[1]["registry"]
        family = next(f for f in registry.collect() if f.name == "daily_briefing_last_post_ts")
        assert family.samples[0].value == 1234567890.0
        assert family.samples[0].labels == {"mode": "weekly"}

    def test_unreachable_pushgateway_raises(self) -> None:
        with patch(
            "workflow_platform.metrics.push_to_gateway",
            side_effect=ConnectionError("refused"),
        ):
            with pytest.raises(ConnectionError):
                push_briefing_post("morning")
