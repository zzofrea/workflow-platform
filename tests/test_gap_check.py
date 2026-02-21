"""Tests for pipeline gap detection.

Maps to Phase 2 acceptance specs 1-4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from workflow_notify.config import NotifyConfig

from workflow_platform.gap_check import check_gap


@pytest.fixture()
def notify_config(tmp_path: object) -> NotifyConfig:
    """Config with vault pointed at a temp dir."""
    from pathlib import Path

    vault = Path(str(tmp_path)) / "monitoring"
    vault.mkdir()
    return NotifyConfig(
        discord_webhook_url="https://discord.com/api/webhooks/fake/token",
        gmail_sender_email="",
        gmail_sender_password="",
        vault_path=str(vault),
    )


# -- Spec 1: Gap detection fires warning when stale --


class TestStaleGap:
    """Last successful run >36h ago -> warning fires."""

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_fires_warning_when_stale(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        # Last success was 48 hours ago
        mock_query.return_value = datetime.now(UTC) - timedelta(hours=48)

        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            threshold_hours=36.0,
            notify_config=notify_config,
        )

        assert result["status"] == "stale"
        assert result["gap_hours"] > 36.0
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] == "warning"
        assert call_kwargs["service"] == "bid-scraper"

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_includes_last_success_time(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        ts = datetime.now(UTC) - timedelta(hours=48)
        mock_query.return_value = ts

        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )

        assert result["last_success"] is not None
        # The notification message should mention when the last run was
        call_kwargs = mock_fanout.call_args.kwargs
        assert "48" in call_kwargs["message"] or "last" in call_kwargs["message"].lower()


# -- Spec 2: No warning when recent run exists --


class TestRecentRun:
    """Last successful run 12h ago -> no notification."""

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_no_notification(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        mock_query.return_value = datetime.now(UTC) - timedelta(hours=12)

        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )

        assert result["status"] == "ok"
        assert result["gap_hours"] < 36.0
        mock_fanout.assert_not_called()


# -- Spec 3: Warning when no successful runs exist --


class TestNoRuns:
    """No rows with status='success' -> warning."""

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_fires_warning_no_runs(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        mock_query.return_value = None

        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )

        assert result["status"] == "no_runs"
        assert result["last_success"] is None
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] == "warning"
        assert "no successful runs" in call_kwargs["message"].lower()


# -- Spec 4: Non-fatal on DB errors --


class TestDbError:
    """DB unreachable -> critical notification, no unhandled exception."""

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_fires_critical_on_db_error(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        mock_query.side_effect = ConnectionRefusedError("Connection refused")

        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )

        assert result["status"] == "db_error"
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args.kwargs
        assert call_kwargs["severity"] == "critical"

    @patch("workflow_platform.gap_check._query_last_success")
    @patch("workflow_platform.gap_check.fanout")
    def test_no_unhandled_exception(
        self, mock_fanout: MagicMock, mock_query: MagicMock, notify_config: NotifyConfig
    ) -> None:
        mock_query.side_effect = OSError("Network unreachable")

        # Should not raise
        result = check_gap(
            service="bid-scraper",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )
        assert result["status"] == "db_error"
        assert "error" in result


# -- Edge case: Unknown service --


class TestUnknownService:
    """Unknown service name -> error result, no crash."""

    def test_returns_unknown_status(self, notify_config: NotifyConfig) -> None:
        result = check_gap(
            service="nonexistent-service",
            db_url="postgresql://fake:fake@localhost/fake",
            notify_config=notify_config,
        )
        assert result["status"] == "unknown_service"
