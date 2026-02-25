"""Tests for the auditor v2 pipeline: temp network bridge + direct DB access.

Tests verify the host-side orchestration logic (network lifecycle, env var
construction, credential parsing, cleanup). Does NOT run actual containers.
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from workflow_platform.auditor import (
    ALLOWED_TOOLS_V2,
    _build_docker_cmd,
    _check_db_running,
    _parse_credentials,
    _resolve_container_names,
    run_audit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def access_doc_password(tmp_path: Path) -> Path:
    """Access doc for bid-scraper with real password."""
    doc = tmp_path / "access.md"
    doc.write_text(
        "## Database\n\n"
        "- Host: gov-bid-postgres\n"
        "- Port: 5432\n"
        "- Database: govbids\n"
        "- User: auditor_ro\n"
        "- Password: auditor_ro_readonly\n"
    )
    return doc


@pytest.fixture()
def access_doc_trust(tmp_path: Path) -> Path:
    """Access doc for ETL with trust auth."""
    doc = tmp_path / "access.md"
    doc.write_text(
        "## Database\n\n"
        "- Host: ds-etl-postgres\n"
        "- Port: 5432\n"
        "- Database: defendershield\n"
        "- User: auditor_ro\n"
        "- Password: (none -- trust auth on internal Docker network)\n"
    )
    return doc


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    """Minimal spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text(
        "## Scenario 1: Database connectivity\n"
        "GIVEN a running database.\n"
        "WHEN connecting.\n"
        "THEN it responds.\n"
    )
    return spec


# ---------------------------------------------------------------------------
# Credential parsing
# ---------------------------------------------------------------------------


class TestParseCredentials:
    def test_password_auth_includes_password(self, access_doc_password: Path) -> None:
        creds = _parse_credentials(str(access_doc_password))
        assert creds["host"] == "gov-bid-postgres"
        assert creds["password"] == "auditor_ro_readonly"
        assert creds["database"] == "govbids"
        assert creds["user"] == "auditor_ro"
        assert creds["port"] == "5432"

    def test_trust_auth_omits_password(self, access_doc_trust: Path) -> None:
        creds = _parse_credentials(str(access_doc_trust))
        assert creds["host"] == "ds-etl-postgres"
        assert creds["password"] == ""
        assert creds["database"] == "defendershield"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        creds = _parse_credentials(str(tmp_path / "nonexistent.md"))
        assert creds == {}


# ---------------------------------------------------------------------------
# Docker command construction
# ---------------------------------------------------------------------------


class TestBuildV2DockerCmd:
    def test_pg_env_vars_in_docker_cmd(self) -> None:
        """Verify all PG env vars are passed to the container."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="bid-scraper",
            mode="prod",
            network="audit-bid-scraper-abc123",
            creds={
                "host": "gov-bid-postgres",
                "port": "5432",
                "user": "auditor_ro",
                "password": "auditor_ro_readonly",
                "database": "govbids",
            },
        )
        env_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert "PGHOST=gov-bid-postgres" in env_pairs
        assert "PGPORT=5432" in env_pairs
        assert "PGUSER=auditor_ro" in env_pairs
        assert "PGPASSWORD=auditor_ro_readonly" in env_pairs
        assert "PGDATABASE=govbids" in env_pairs

    def test_trust_auth_omits_pgpassword(self) -> None:
        """When password is empty (trust auth), PGPASSWORD should not be set."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="etl",
            mode="prod",
            network="audit-etl-abc123",
            creds={
                "host": "ds-etl-postgres",
                "port": "5432",
                "user": "auditor_ro",
                "password": "",
                "database": "defendershield",
            },
        )
        env_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert "PGHOST=ds-etl-postgres" in env_pairs
        pgpassword_entries = [e for e in env_pairs if e.startswith("PGPASSWORD")]
        assert len(pgpassword_entries) == 0

    def test_not_on_dokploy_network(self) -> None:
        """The v2 auditor must never use dokploy-network."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
            network="audit-test-abc123",
            creds={"host": "db", "port": "5432", "user": "u", "password": "", "database": "d"},
        )
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "audit-test-abc123"
        assert "dokploy-network" not in " ".join(cmd)

    def test_scoped_allowed_tools(self) -> None:
        """Verify the ALLOWED_TOOLS_V2 constant is correct."""
        assert "Read" in ALLOWED_TOOLS_V2
        assert "Bash(psql*)" in ALLOWED_TOOLS_V2
        assert "Bash(python3*)" in ALLOWED_TOOLS_V2
        assert "Bash(date*)" in ALLOWED_TOOLS_V2
        # Must NOT allow curl/wget/nc
        assert "curl" not in ALLOWED_TOOLS_V2
        assert "wget" not in ALLOWED_TOOLS_V2

    def test_max_turns_default_50(self) -> None:
        """Default max_turns in the docker cmd should be 50."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
            network="audit-test-abc",
            creds={"host": "db", "port": "5432", "user": "u", "password": "", "database": "d"},
        )
        env_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert "AUDITOR_MAX_TURNS=50" in env_pairs

    def test_v2_stage_env_var(self) -> None:
        """The container must have AUDITOR_STAGE=v2."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
            network="audit-test-abc",
            creds={"host": "db", "port": "5432", "user": "u", "password": "", "database": "d"},
        )
        env_pairs = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert "AUDITOR_STAGE=v2" in env_pairs

    def test_cap_drop_all(self) -> None:
        """Container must drop all capabilities."""
        cmd = _build_docker_cmd(
            "/tmp/input",
            "/tmp/output",
            service="test",
            network="audit-test-abc",
            creds={"host": "db", "port": "5432", "user": "u", "password": "", "database": "d"},
        )
        assert "--cap-drop" in cmd
        idx = cmd.index("--cap-drop")
        assert cmd[idx + 1] == "ALL"


# ---------------------------------------------------------------------------
# Full pipeline tests (mocked Docker)
# ---------------------------------------------------------------------------


class TestRunAuditV2:
    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    @patch("workflow_platform.auditor._check_db_running", return_value=True)
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_successful_audit_complete_report(
        self,
        mock_image: MagicMock,
        mock_resolve: MagicMock,
        mock_check: MagicMock,
        mock_run: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """Full happy-path: network create, connect, run container, collect, cleanup."""
        archive_dir = str(tmp_path / "archive")
        output_report = {
            "overall": "pass",
            "service": "bid-scraper",
            "mode": "prod",
            "summary": "All scenarios verified",
            "scenarios": [{"id": 1, "status": "pass"}],
            "scenarios_total": 1,
            "scenarios_pass": 1,
            "scenarios_fail": 0,
            "scenarios_error": 0,
        }

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            # Write report.json when the auditor container runs
            if cmd[0:2] == ["docker", "run"]:
                # Find output dir from -v mounts
                for i, arg in enumerate(cmd):
                    if arg == "-v" and "/audit/output:rw" in cmd[i + 1]:
                        host_out = cmd[i + 1].split(":")[0]
                        report_path = Path(host_out) / "report.json"
                        report_path.write_text(json.dumps(output_report))
                        break
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        report = run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=archive_dir,
            notify=True,
        )

        assert report["overall"] == "pass"
        assert (Path(archive_dir) / "report.json").exists()
        mock_notify.assert_called_once()

    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    @patch("workflow_platform.auditor._check_db_running", return_value=True)
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_temp_network_lifecycle(
        self,
        mock_image: MagicMock,
        mock_resolve: MagicMock,
        mock_check: MagicMock,
        mock_run: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """Verify network create, connect, disconnect, and rm are all called."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=str(tmp_path / "archive"),
            notify=False,
        )

        all_calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("network" in c and "create" in c for c in all_calls_str)
        assert any("network" in c and "connect" in c for c in all_calls_str)
        assert any("network" in c and "disconnect" in c for c in all_calls_str)
        assert any("network" in c and "rm" in c for c in all_calls_str)

    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._check_db_running", return_value=False)
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_db_not_running_fails_early(
        self,
        mock_image: MagicMock,
        mock_check: MagicMock,
        mock_resolve: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """When DB is not running, fail before launching the auditor container."""
        report = run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=str(tmp_path / "archive"),
            notify=True,
        )

        assert report["overall"] == "error"
        assert "not running" in report["summary"]
        mock_notify.assert_called_once()

    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    @patch("workflow_platform.auditor._check_db_running", return_value=True)
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_timeout_kills_and_cleans_up(
        self,
        mock_image: MagicMock,
        mock_resolve: MagicMock,
        mock_check: MagicMock,
        mock_run: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """TimeoutExpired triggers kill + network cleanup."""

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0:2] == ["docker", "run"]:
                raise sp.TimeoutExpired(cmd=cmd, timeout=10)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        report = run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=str(tmp_path / "archive"),
            notify=True,
            total_timeout=10,
        )

        assert report["overall"] == "error"
        assert "timed out" in report["summary"].lower()
        # Verify cleanup happened (disconnect + rm)
        all_calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("network" in c and "disconnect" in c for c in all_calls_str)
        assert any("network" in c and "rm" in c for c in all_calls_str)

    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    @patch("workflow_platform.auditor._check_db_running", return_value=True)
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_network_alias_matches_hostname(
        self,
        mock_image: MagicMock,
        mock_resolve: MagicMock,
        mock_check: MagicMock,
        mock_run: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """The --alias in the connect call must match the DB hostname."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=str(tmp_path / "archive"),
            notify=False,
        )

        # Find the network connect call and verify alias
        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else []
            if isinstance(args, list) and "network" in args and "connect" in args:
                assert "--alias" in args
                alias_idx = args.index("--alias")
                assert args[alias_idx + 1] == "gov-bid-postgres"
                break
        else:
            pytest.fail("No network connect call found")

    @patch("workflow_platform.auditor.route_notifications")
    @patch("workflow_platform.auditor.subprocess.run")
    @patch("workflow_platform.auditor._check_db_running", return_value=True)
    @patch("workflow_platform.auditor._resolve_container_names", return_value={"gov-bid-postgres": "real-ctr"})
    @patch("workflow_platform.auditor._image_exists_locally", return_value=True)
    def test_cleanup_on_container_failure(
        self,
        mock_image: MagicMock,
        mock_resolve: MagicMock,
        mock_check: MagicMock,
        mock_run: MagicMock,
        mock_notify: MagicMock,
        spec_file: Path,
        access_doc_password: Path,
        tmp_path: Path,
    ) -> None:
        """Non-zero exit from the auditor container still cleans up the network."""

        def side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0:2] == ["docker", "run"]:
                return MagicMock(returncode=1, stdout="", stderr="container failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        report = run_audit(
            str(spec_file),
            str(access_doc_password),
            "bid-scraper",
            "prod",
            archive_dir=str(tmp_path / "archive"),
            notify=False,
        )

        # Report should indicate error (no report.json produced)
        assert report["overall"] == "error"
        # Network cleanup should still happen
        all_calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("network" in c and "disconnect" in c for c in all_calls_str)
        assert any("network" in c and "rm" in c for c in all_calls_str)

    def test_report_format_compatible(self, tmp_path: Path) -> None:
        """Verify the error report format has all required fields."""
        # Test with a DB-not-running scenario to get a report without Docker
        spec = tmp_path / "spec.md"
        spec.write_text("GIVEN x. WHEN y. THEN z.")
        access = tmp_path / "access.md"
        access.write_text("- Host: test-db\n- Port: 5432\n- Database: testdb\n- User: u\n- Password: p\n")

        with (
            patch("workflow_platform.auditor._image_exists_locally", return_value=True),
            patch("workflow_platform.auditor._resolve_container_names", return_value={"test-db": "test-db"}),
            patch("workflow_platform.auditor._check_db_running", return_value=False),
            patch("workflow_platform.auditor.route_notifications"),
        ):
            report = run_audit(
                str(spec),
                str(access),
                "test",
                "prod",
                archive_dir=str(tmp_path / "archive"),
                notify=False,
            )

        # Required fields for notification pipeline
        assert "overall" in report
        assert "service" in report
        assert "mode" in report
        assert "summary" in report
        assert "scenarios" in report
        assert isinstance(report["scenarios"], list)
