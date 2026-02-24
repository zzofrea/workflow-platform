"""Acceptance tests for the two-stage auditor hardening.

Generated from specs/two_stage_auditor.md. These test the WHAT (external behavior)
not the HOW (implementation). All tests should fail initially (red) and pass
after implementation (green).

Tests mock Docker and Claude CLI interactions -- they do NOT run real containers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def access_doc_bid_scraper(tmp_path: Path) -> Path:
    """Access document for bid-scraper with DB host and URL allowlist."""
    doc = tmp_path / "bid-scraper-access.md"
    doc.write_text(
        "## Database\n\n"
        "- Host: bid-scraper-postgres\n"
        "- Port: 5432\n"
        "- Database: bidscraper\n"
        "- User: auditor_ro\n"
        "- Password: auditor_ro_readonly\n\n"
        "## Tables\n\n"
        "### sources\n### opportunities\n### contracts\n### vendors\n### scrape_runs\n\n"
        "## Allowed URLs\n\n"
        "| URL | Purpose |\n"
        "|-----|--------|\n"
        "| https://hillsboroughcounty.bonfirehub.com/PublicPortal/"
        "getOpenPublicOpportunitiesSectionData | Hillsborough open |\n"
        "| https://hillsboroughcounty.bonfirehub.com/PublicPortal/"
        "getPastPublicOpportunitiesSectionData | Hillsborough past |\n"
        "| https://hillsboroughcounty.bonfirehub.com/PublicPortal/"
        "getPublicContractsSectionData | Hillsborough contracts |\n"
        "| https://pascocountyfl.bonfirehub.com/PublicPortal/"
        "getOpenPublicOpportunitiesSectionData | Pasco open |\n"
        "| https://pascocountyfl.bonfirehub.com/PublicPortal/"
        "getPastPublicOpportunitiesSectionData | Pasco past |\n"
        "| https://pascocountyfl.bonfirehub.com/PublicPortal/"
        "getPublicContractsSectionData | Pasco contracts |\n"
    )
    return doc


@pytest.fixture()
def access_doc_etl(tmp_path: Path) -> Path:
    """Access document for defendershield-etl with DB host and empty URL allowlist."""
    doc = tmp_path / "etl-access.md"
    doc.write_text(
        "## Database\n\n"
        "- Host: ds-etl-postgres\n"
        "- Port: 5432\n"
        "- Database: defendershield\n"
        "- User: auditor_ro\n\n"
        "## Tables\n\n"
        "### silver.fact_sales_items\n"
        "### gold.forecast_depletion\n\n"
        "## Allowed URLs\n\n"
        "No HTTP endpoints are used by this service. "
        "The URL allowlist is empty.\n"
    )
    return doc


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    """Minimal behavioral spec for testing."""
    spec = tmp_path / "spec.md"
    spec.write_text(
        "; Data freshness check.\n"
        "GIVEN the scraper runs daily.\n"
        "WHEN checking the last scrape timestamp.\n"
        "THEN the most recent scrape is within 48 hours.\n"
    )
    return spec


def _make_query_plan(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Helper to build a query plan dict."""
    return {"queries": entries}


def _psql_entry(host: str, table: str) -> dict[str, str]:
    """Helper to build a psql query plan entry."""
    return {
        "type": "psql",
        "host": host,
        "query": f"SELECT * FROM {table};",
    }


def _curl_entry(url: str) -> dict[str, str]:
    """Helper to build a curl query plan entry."""
    return {
        "type": "curl",
        "url": url,
    }


# ---------------------------------------------------------------------------
# Spec 1: Planner produces a valid query plan without network access
# ---------------------------------------------------------------------------


class TestPlannerNetworkIsolation:
    """Spec 1: The planner container must NOT be on dokploy-network."""

    def test_planner_container_uses_bridge_not_dokploy(self) -> None:
        """The planner uses bridge network (API access) but never dokploy-network."""
        from workflow_platform.two_stage_auditor import build_planner_cmd

        cmd = build_planner_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        assert "--network" in cmd
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "bridge"
        cmd_str = " ".join(cmd)
        assert "dokploy-network" not in cmd_str

    def test_planner_does_not_use_dangerously_skip_permissions(self) -> None:
        """The planner must NOT use --dangerously-skip-permissions."""
        from workflow_platform.two_stage_auditor import build_planner_cmd

        cmd = build_planner_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        cmd_str = " ".join(cmd)
        assert "--dangerously-skip-permissions" not in cmd_str

    def test_planner_allowed_tools_read_only(self) -> None:
        """The planner must only have Read tool access, no Bash."""
        from workflow_platform.two_stage_auditor import build_planner_cmd

        cmd = build_planner_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        cmd_str = " ".join(cmd)
        assert "Bash" not in cmd_str
        # Should have --allowedTools with Read
        assert "--allowedTools" in cmd


# ---------------------------------------------------------------------------
# Spec 2: Validator accepts a conforming query plan
# ---------------------------------------------------------------------------


class TestValidatorAccepts:
    """Spec 2: Valid plans pass through unchanged."""

    def test_accepts_valid_psql_plan(self, access_doc_bid_scraper: Path) -> None:
        """A plan with valid SELECT * FROM queries and allowed hosts passes."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
                _psql_entry("bid-scraper-postgres", "opportunities"),
                _psql_entry("bid-scraper-postgres", "contracts"),
                _psql_entry("bid-scraper-postgres", "vendors"),
                _psql_entry("bid-scraper-postgres", "scrape_runs"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is True
        assert result.rejection_reason is None

    def test_accepts_schema_qualified_table(self, access_doc_etl: Path) -> None:
        """Schema-qualified table names like gold.forecast_depletion are valid."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _psql_entry("ds-etl-postgres", "silver.fact_sales_items"),
                _psql_entry("ds-etl-postgres", "gold.forecast_depletion"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_etl))
        assert result.valid is True

    def test_accepts_valid_curl_plan(self, access_doc_bid_scraper: Path) -> None:
        """A plan with curl URLs that exactly match the allowlist passes."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _curl_entry(
                    "https://hillsboroughcounty.bonfirehub.com/PublicPortal/"
                    "getOpenPublicOpportunitiesSectionData"
                ),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is True


# ---------------------------------------------------------------------------
# Spec 3: Validator rejects unauthorized host
# ---------------------------------------------------------------------------


class TestValidatorRejectsHost:
    """Spec 3: Plans targeting hosts not in the access doc are rejected."""

    def test_rejects_unauthorized_host(self, access_doc_bid_scraper: Path) -> None:
        """A plan targeting ds-etl-postgres when only bid-scraper-postgres is allowed fails."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _psql_entry("ds-etl-postgres", "silver.fact_sales_items"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False
        assert "ds-etl-postgres" in (result.rejection_reason or "")

    def test_rejects_mixed_valid_invalid_hosts(self, access_doc_bid_scraper: Path) -> None:
        """Even one unauthorized host in a multi-entry plan causes full rejection."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
                _psql_entry("dokploy-postgres", "apikey"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False


# ---------------------------------------------------------------------------
# Spec 4: Validator rejects non-allowlisted SQL shape
# ---------------------------------------------------------------------------


class TestValidatorRejectsSQL:
    """Spec 4: Only SELECT * FROM table is permitted."""

    def test_rejects_where_clause(self, access_doc_bid_scraper: Path) -> None:
        """SELECT with WHERE is not permitted even though it's read-only."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": "SELECT * FROM opportunities WHERE status_id = 1;",
                }
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_join(self, access_doc_bid_scraper: Path) -> None:
        """JOINs are not permitted."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": (
                        "SELECT * FROM opportunities JOIN sources"
                        " ON opportunities.source_id = sources.source_id;"
                    ),
                }
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_dblink(self, access_doc_bid_scraper: Path) -> None:
        """dblink-based attacks are rejected by the regex."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": "SELECT * FROM dblink('host=other-db', 'DELETE FROM users');",
                }
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_insert(self, access_doc_bid_scraper: Path) -> None:
        """Write operations are rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": "INSERT INTO sources (source_id) VALUES ('evil');",
                }
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_drop(self, access_doc_bid_scraper: Path) -> None:
        """DDL is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": "DROP TABLE sources;",
                }
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_accepts_select_star_with_optional_semicolon(
        self, access_doc_bid_scraper: Path
    ) -> None:
        """SELECT * FROM table with or without trailing semicolon both pass."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan_with = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        plan_without = _make_query_plan(
            [
                {
                    "type": "psql",
                    "host": "bid-scraper-postgres",
                    "query": "SELECT * FROM sources",
                }
            ]
        )
        assert validate_query_plan(plan_with, str(access_doc_bid_scraper)).valid is True
        assert validate_query_plan(plan_without, str(access_doc_bid_scraper)).valid is True


# ---------------------------------------------------------------------------
# Spec 5: Validator rejects unauthorized URL
# ---------------------------------------------------------------------------


class TestValidatorRejectsURL:
    """Spec 5: Curl URLs must exactly match the access doc allowlist."""

    def test_rejects_unauthorized_url(self, access_doc_bid_scraper: Path) -> None:
        """A curl entry targeting an external URL is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _curl_entry("https://evil.com/exfiltrate"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False
        assert "evil.com" in (result.rejection_reason or "")

    def test_rejects_partial_url_match(self, access_doc_bid_scraper: Path) -> None:
        """A URL that is a substring of an allowed URL but not exact is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _curl_entry("https://hillsboroughcounty.bonfirehub.com/PublicPortal/"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_url_with_extra_params(self, access_doc_bid_scraper: Path) -> None:
        """Adding query parameters to an allowed URL is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _curl_entry(
                    "https://hillsboroughcounty.bonfirehub.com/PublicPortal/"
                    "getOpenPublicOpportunitiesSectionData?inject=true"
                ),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False


# ---------------------------------------------------------------------------
# Spec 6: Validator rejects curl when URL allowlist is empty
# ---------------------------------------------------------------------------


class TestValidatorRejectsCurlEmptyAllowlist:
    """Spec 6: Services with no HTTP endpoints reject all curl entries."""

    def test_rejects_any_curl_for_etl(self, access_doc_etl: Path) -> None:
        """Any curl entry for ETL (empty URL allowlist) is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _curl_entry("https://anything.com/"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_etl))
        assert result.valid is False
        assert (
            "not permitted" in (result.rejection_reason or "").lower()
            or "url" in (result.rejection_reason or "").lower()
        )

    def test_psql_still_works_with_empty_url_list(self, access_doc_etl: Path) -> None:
        """PSql queries still work even when the URL allowlist is empty."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = _make_query_plan(
            [
                _psql_entry("ds-etl-postgres", "silver.fact_sales_items"),
            ]
        )
        result = validate_query_plan(plan, str(access_doc_etl))
        assert result.valid is True


# ---------------------------------------------------------------------------
# Spec 7: Executor runs on isolated network
# ---------------------------------------------------------------------------


class TestExecutorNetworkIsolation:
    """Spec 7: The executor creates and tears down a temporary Docker network."""

    @patch("workflow_platform.two_stage_auditor.subprocess.run")
    def test_creates_temp_network(self, mock_run: MagicMock) -> None:
        """The executor creates a temporary Docker network before running queries."""
        from workflow_platform.two_stage_auditor import run_executor

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        credentials = {
            "bid-scraper-postgres": {
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "bidscraper",
                "port": "5432",
            }
        }

        run_executor(plan, credentials, output_dir="/tmp/output")

        # Check that docker network create was called
        all_calls = [str(c) for c in mock_run.call_args_list]
        network_create_calls = [c for c in all_calls if "network" in c and "create" in c]
        assert len(network_create_calls) >= 1, "Expected docker network create call"

    @patch("workflow_platform.two_stage_auditor.subprocess.run")
    def test_tears_down_temp_network(self, mock_run: MagicMock) -> None:
        """The temporary network is removed after execution completes."""
        from workflow_platform.two_stage_auditor import run_executor

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        credentials = {
            "bid-scraper-postgres": {
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "bidscraper",
                "port": "5432",
            }
        }

        run_executor(plan, credentials, output_dir="/tmp/output")

        # Check that docker network rm was called
        all_calls = [str(c) for c in mock_run.call_args_list]
        network_rm_calls = [c for c in all_calls if "network" in c and "rm" in c]
        assert len(network_rm_calls) >= 1, "Expected docker network rm call"

    @patch("workflow_platform.two_stage_auditor.subprocess.run")
    def test_executor_not_on_dokploy_network(self, mock_run: MagicMock) -> None:
        """The executor container must never join dokploy-network."""
        from workflow_platform.two_stage_auditor import run_executor

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        credentials = {
            "bid-scraper-postgres": {
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "bidscraper",
                "port": "5432",
            }
        }

        run_executor(plan, credentials, output_dir="/tmp/output")

        # The resolver reads dokploy-network metadata, but the executor
        # must never connect to or create containers on dokploy-network.
        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get("args", [])
            if isinstance(args, list) and "network" in args and "connect" in args:
                assert "dokploy-network" not in args, (
                    "Executor must not connect containers to dokploy-network"
                )


# ---------------------------------------------------------------------------
# Spec 8: Executor fails fast and cleans up
# ---------------------------------------------------------------------------


class TestExecutorFailFast:
    """Spec 8: DB unavailability causes immediate failure with cleanup."""

    @patch("workflow_platform.two_stage_auditor.subprocess.run")
    def test_fails_on_connection_error(self, mock_run: MagicMock) -> None:
        """When the database is unreachable, the executor fails immediately."""
        from workflow_platform.two_stage_auditor import ExecutorError, run_executor

        # Resolver inspect, then: network create, connect, query fails, cleanup
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # resolver: network inspect
            MagicMock(returncode=0),  # network create
            MagicMock(returncode=0),  # network connect target
            MagicMock(returncode=2, stderr="connection refused"),  # psql fails
            MagicMock(returncode=0),  # network disconnect
            MagicMock(returncode=0),  # network rm
        ]

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        credentials = {
            "bid-scraper-postgres": {
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "bidscraper",
                "port": "5432",
            }
        }

        with pytest.raises(ExecutorError):
            run_executor(plan, credentials, output_dir="/tmp/output")

    @patch("workflow_platform.two_stage_auditor.subprocess.run")
    def test_cleans_up_network_on_failure(self, mock_run: MagicMock) -> None:
        """The temp network is removed even when queries fail."""
        from workflow_platform.two_stage_auditor import ExecutorError, run_executor

        # Resolver inspect, then: network create, connect, query fails, cleanup
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # resolver: network inspect
            MagicMock(returncode=0),  # network create
            MagicMock(returncode=0),  # network connect target
            MagicMock(returncode=2, stderr="connection refused"),  # psql fails
            MagicMock(returncode=0),  # network disconnect
            MagicMock(returncode=0),  # network rm
        ]

        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        credentials = {
            "bid-scraper-postgres": {
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "bidscraper",
                "port": "5432",
            }
        }

        with pytest.raises(ExecutorError):
            run_executor(plan, credentials, output_dir="/tmp/output")

        # Network cleanup should still happen
        all_calls = [str(c) for c in mock_run.call_args_list]
        network_rm_calls = [c for c in all_calls if "network" in c and "rm" in c]
        assert len(network_rm_calls) >= 1


# ---------------------------------------------------------------------------
# Spec 9: Analyzer produces report without network access
# ---------------------------------------------------------------------------


class TestAnalyzerNetworkIsolation:
    """Spec 9: The analyzer container must NOT be on dokploy-network."""

    def test_analyzer_container_uses_bridge_not_dokploy(self) -> None:
        """The analyzer uses bridge network (API access) but never dokploy-network."""
        from workflow_platform.two_stage_auditor import build_analyzer_cmd

        cmd = build_analyzer_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        assert "--network" in cmd
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "bridge"
        cmd_str = " ".join(cmd)
        assert "dokploy-network" not in cmd_str

    def test_analyzer_does_not_use_dangerously_skip_permissions(self) -> None:
        """The analyzer must NOT use --dangerously-skip-permissions."""
        from workflow_platform.two_stage_auditor import build_analyzer_cmd

        cmd = build_analyzer_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        cmd_str = " ".join(cmd)
        assert "--dangerously-skip-permissions" not in cmd_str


# ---------------------------------------------------------------------------
# Spec 10: Full pipeline backward compatibility
# ---------------------------------------------------------------------------


class TestPipelineBackwardCompat:
    """Spec 10: The pipeline produces output identical to the old auditor."""

    @patch("workflow_platform.two_stage_auditor.run_analyzer")
    @patch("workflow_platform.two_stage_auditor.run_executor")
    @patch("workflow_platform.two_stage_auditor.validate_query_plan")
    @patch("workflow_platform.two_stage_auditor.run_planner")
    def test_report_archived_to_standard_path(
        self,
        mock_planner: MagicMock,
        mock_validator: MagicMock,
        mock_executor: MagicMock,
        mock_analyzer: MagicMock,
        spec_file: Path,
        access_doc_bid_scraper: Path,
        tmp_path: Path,
    ) -> None:
        """Report is archived to ~/audit-reports/{service}/{mode}_{timestamp}/."""
        from workflow_platform.two_stage_auditor import ValidationResult, run_two_stage_audit

        mock_planner.return_value = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        mock_validator.return_value = ValidationResult(valid=True, rejection_reason=None)
        mock_executor.return_value = {"sources": [{"source_id": "hillsborough"}]}
        mock_analyzer.return_value = {
            "overall": "pass",
            "service": "bid-scraper",
            "summary": "All scenarios verified",
            "scenarios": [{"id": 1, "status": "pass"}],
        }

        archive_dir = str(tmp_path / "archive")
        report = run_two_stage_audit(
            spec_path=str(spec_file),
            access_path=str(access_doc_bid_scraper),
            service="bid-scraper",
            mode="prod",
            archive_dir=archive_dir,
            notify=False,
        )

        assert report["overall"] == "pass"
        assert (Path(archive_dir) / "report.json").exists()

    @patch("workflow_platform.two_stage_auditor.run_analyzer")
    @patch("workflow_platform.two_stage_auditor.run_executor")
    @patch("workflow_platform.two_stage_auditor.validate_query_plan")
    @patch("workflow_platform.two_stage_auditor.run_planner")
    def test_notification_sent_on_completion(
        self,
        mock_planner: MagicMock,
        mock_validator: MagicMock,
        mock_executor: MagicMock,
        mock_analyzer: MagicMock,
        spec_file: Path,
        access_doc_bid_scraper: Path,
        tmp_path: Path,
    ) -> None:
        """Notifications are sent through the same channels as the old auditor."""
        from workflow_platform.two_stage_auditor import ValidationResult, run_two_stage_audit

        mock_planner.return_value = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        mock_validator.return_value = ValidationResult(valid=True, rejection_reason=None)
        mock_executor.return_value = {}
        mock_analyzer.return_value = {
            "overall": "pass",
            "service": "bid-scraper",
            "summary": "ok",
            "scenarios": [],
        }

        with patch("workflow_platform.two_stage_auditor.route_notifications") as mock_notify:
            run_two_stage_audit(
                spec_path=str(spec_file),
                access_path=str(access_doc_bid_scraper),
                service="bid-scraper",
                mode="prod",
                archive_dir=str(tmp_path / "archive"),
                notify=True,
            )
            mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# Spec 11: Pipeline timeout enforcement
# ---------------------------------------------------------------------------


class TestPipelineTimeout:
    """Spec 11: The 20-minute total timeout is enforced."""

    @patch("workflow_platform.two_stage_auditor.run_planner")
    def test_timeout_kills_and_notifies(
        self,
        mock_planner: MagicMock,
        spec_file: Path,
        access_doc_bid_scraper: Path,
        tmp_path: Path,
    ) -> None:
        """When the pipeline exceeds the timeout, it fails with a timeout reason."""
        import subprocess as sp

        from workflow_platform.two_stage_auditor import run_two_stage_audit

        mock_planner.side_effect = sp.TimeoutExpired(cmd=["docker", "run"], timeout=1200)

        with patch("workflow_platform.two_stage_auditor.route_notifications") as mock_notify:
            report = run_two_stage_audit(
                spec_path=str(spec_file),
                access_path=str(access_doc_bid_scraper),
                service="bid-scraper",
                mode="prod",
                archive_dir=str(tmp_path / "archive"),
                notify=True,
                total_timeout=1200,
            )

        assert report["overall"] == "error"
        assert "timeout" in report["summary"].lower() or "timed out" in report["summary"].lower()
        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# Spec 12: Credentials never in planner or analyzer context
# ---------------------------------------------------------------------------


class TestCredentialIsolation:
    """Spec 12: Claude never sees database credentials."""

    def test_planner_input_has_no_credentials_env(self) -> None:
        """The planner container command must not include credential env vars."""
        from workflow_platform.two_stage_auditor import build_planner_cmd

        cmd = build_planner_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        cmd_str = " ".join(cmd)
        assert "auditor_ro_readonly" not in cmd_str
        assert "PGPASSWORD" not in cmd_str

    def test_analyzer_input_has_no_credentials_env(self) -> None:
        """The analyzer container command must not include credential env vars."""
        from workflow_platform.two_stage_auditor import build_analyzer_cmd

        cmd = build_analyzer_cmd(
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            service="bid-scraper",
        )
        cmd_str = " ".join(cmd)
        assert "auditor_ro_readonly" not in cmd_str
        assert "PGPASSWORD" not in cmd_str

    def test_query_plan_contains_no_credentials(self) -> None:
        """The query plan schema has host and table only, no credentials."""
        plan = _make_query_plan(
            [
                _psql_entry("bid-scraper-postgres", "sources"),
            ]
        )
        plan_str = json.dumps(plan)
        assert "password" not in plan_str.lower()
        assert "auditor_ro_readonly" not in plan_str


# ---------------------------------------------------------------------------
# Spec 13: Old single-stage code removed
# ---------------------------------------------------------------------------


class TestOldCodeRemoval:
    """Spec 13: No --dangerously-skip-permissions in the codebase."""

    def test_no_dangerously_skip_permissions_in_source(self) -> None:
        """The flag --dangerously-skip-permissions must not appear in any source file."""
        src_dir = Path(__file__).resolve().parent.parent / "src"
        auditor_dir = Path(__file__).resolve().parent.parent / "auditor"

        for search_dir in [src_dir, auditor_dir]:
            if not search_dir.exists():
                continue
            for py_file in search_dir.rglob("*.py"):
                content = py_file.read_text()
                assert "dangerously-skip-permissions" not in content, (
                    f"Found --dangerously-skip-permissions in {py_file}"
                )
                assert "dangerously_skip_permissions" not in content, (
                    f"Found dangerously_skip_permissions in {py_file}"
                )


# ---------------------------------------------------------------------------
# Validator regex edge cases (defense in depth)
# ---------------------------------------------------------------------------


class TestSQLAllowlistRegex:
    """Additional regex validation tests beyond the spec scenarios."""

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM sources;",
            "SELECT * FROM sources",
            "SELECT * FROM gold.forecast_depletion;",
            "SELECT * FROM silver.fact_sales_items",
            "SELECT * FROM public.bid_opportunities;",
            "SELECT * FROM _internal_table",
        ],
    )
    def test_valid_queries(self, query: str) -> None:
        """These queries must match the strict allowlist regex."""
        from workflow_platform.two_stage_auditor import SQL_ALLOWLIST_PATTERN

        assert re.match(SQL_ALLOWLIST_PATTERN, query), f"Expected valid: {query}"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM sources WHERE 1=1;",
            "SELECT id FROM sources;",
            "SELECT * FROM sources LIMIT 10;",
            "SELECT * FROM sources; DROP TABLE sources;",
            "SELECT * FROM dblink('host=x', 'SELECT 1');",
            "INSERT INTO sources VALUES ('x');",
            "DELETE FROM sources;",
            "UPDATE sources SET source_id='x';",
            "DROP TABLE sources;",
            "COPY sources TO '/tmp/dump';",
            "SELECT * FROM sources UNION SELECT * FROM contracts;",
            "  SELECT * FROM sources;",
            "select * from sources;",
        ],
    )
    def test_invalid_queries(self, query: str) -> None:
        """These queries must NOT match the strict allowlist regex."""
        from workflow_platform.two_stage_auditor import SQL_ALLOWLIST_PATTERN

        assert not re.match(SQL_ALLOWLIST_PATTERN, query), f"Expected invalid: {query}"


# ---------------------------------------------------------------------------
# Validator schema conformance
# ---------------------------------------------------------------------------


class TestQueryPlanSchema:
    """The validator rejects malformed query plans."""

    def test_rejects_missing_queries_key(self, access_doc_bid_scraper: Path) -> None:
        """A plan without the 'queries' key is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan: dict[str, Any] = {"data": []}
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_entry_missing_type(self, access_doc_bid_scraper: Path) -> None:
        """A query entry without 'type' is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = {"queries": [{"host": "bid-scraper-postgres", "query": "SELECT * FROM sources;"}]}
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_unknown_type(self, access_doc_bid_scraper: Path) -> None:
        """A query entry with an unknown type is rejected."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan = {
            "queries": [
                {
                    "type": "ssh",
                    "host": "bid-scraper-postgres",
                    "query": "ls /",
                }
            ]
        }
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False

    def test_rejects_empty_plan(self, access_doc_bid_scraper: Path) -> None:
        """An empty query list is rejected (auditor must collect some data)."""
        from workflow_platform.two_stage_auditor import validate_query_plan

        plan: dict[str, Any] = {"queries": []}
        result = validate_query_plan(plan, str(access_doc_bid_scraper))
        assert result.valid is False
