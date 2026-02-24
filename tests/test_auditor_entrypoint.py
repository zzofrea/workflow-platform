"""Tests for the auditor container entrypoint logic.

Tests report parsing, JSON/markdown report building -- the pieces that
run inside the container. Does NOT test Claude CLI invocation.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the auditor/ directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "auditor"))

import entrypoint  # noqa: E402

# -- Tool permission scoping --


class TestBuildAllowedTools:
    def test_single_host_scopes_psql_and_curl(self) -> None:
        result = entrypoint._build_allowed_tools(["bid-scraper-postgres"])
        assert "Bash(psql:*bid-scraper-postgres*)" in result
        assert "Bash(curl:*bid-scraper-postgres*)" in result
        assert "Bash(date)" in result
        assert "Read" in result
        # Must NOT contain wildcards for arbitrary hosts
        assert "Bash(psql:*),Bash(curl:*)" not in result

    def test_multiple_hosts(self) -> None:
        result = entrypoint._build_allowed_tools(["db-host", "api-host"])
        assert "Bash(psql:*db-host*)" in result
        assert "Bash(curl:*db-host*)" in result
        assert "Bash(psql:*api-host*)" in result
        assert "Bash(curl:*api-host*)" in result

    def test_no_hosts_only_date_and_read(self) -> None:
        result = entrypoint._build_allowed_tools([])
        assert result == "Bash(date),Read"
        assert "psql" not in result
        assert "curl" not in result


# -- Prompt building --


class TestBuildPrompt:
    def test_includes_spec_and_access(self) -> None:
        prompt = entrypoint.build_prompt("GIVEN x.\nWHEN y.\nTHEN z.", "psql -h db -U user")
        assert "GIVEN x" in prompt
        assert "psql -h db -U user" in prompt
        assert "Behavioral Specification" in prompt
        assert "Service Access" in prompt


# -- Report parsing --


class TestParseReport:
    def test_parses_clean_json(self) -> None:
        raw = '{"scenarios": [{"id": 1, "status": "pass"}], "summary": "ok"}'
        result = entrypoint.parse_report(raw)
        assert result is not None
        assert result["scenarios"][0]["status"] == "pass"

    def test_parses_json_in_markdown_fencing(self) -> None:
        raw = 'Here is the report:\n```json\n{"scenarios": [], "summary": "done"}\n```\nEnd.'
        result = entrypoint.parse_report(raw)
        assert result is not None
        assert result["summary"] == "done"

    def test_parses_json_with_surrounding_text(self) -> None:
        raw = (
            "Some preamble\n"
            '{"scenarios": [{"id": 1, "status": "fail"}], "summary": "bad"}\n'
            "More text"
        )
        result = entrypoint.parse_report(raw)
        assert result is not None
        assert result["scenarios"][0]["status"] == "fail"

    def test_returns_none_for_unparseable(self) -> None:
        assert entrypoint.parse_report("This is not JSON at all") is None

    def test_returns_none_for_empty(self) -> None:
        assert entrypoint.parse_report("") is None


# -- JSON report building --


class TestBuildJsonReport:
    def test_all_pass(self) -> None:
        parsed = {
            "scenarios": [
                {"id": 1, "status": "pass", "observation": "ok", "evidence": "row=1"},
                {"id": 2, "status": "pass", "observation": "ok", "evidence": "row=2"},
            ],
            "summary": "All good",
        }
        report = entrypoint.build_json_report(
            parsed=parsed,
            raw_output="",
            model="sonnet",
            mode="build",
            service="bid-scraper",
            duration=45.2,
        )
        assert report["overall"] == "pass"
        assert report["scenarios_pass"] == 2
        assert report["scenarios_fail"] == 0
        assert report["duration_seconds"] == 45.2
        assert report["model"] == "sonnet"
        assert report["service"] == "bid-scraper"

    def test_has_failures(self) -> None:
        parsed = {
            "scenarios": [
                {"id": 1, "status": "pass"},
                {"id": 2, "status": "fail", "observation": "no data"},
            ],
            "summary": "Problems found",
        }
        report = entrypoint.build_json_report(
            parsed=parsed,
            raw_output="",
            model="sonnet",
            mode="prod",
            service="etl",
            duration=30.0,
        )
        assert report["overall"] == "fail"
        assert report["scenarios_fail"] == 1

    def test_incomplete_flag(self) -> None:
        report = entrypoint.build_json_report(
            parsed=None,
            raw_output="partial output...",
            model="sonnet",
            mode="build",
            service="test",
            duration=120.0,
            incomplete=True,
            incomplete_reason="token limit reached",
        )
        assert report["overall"] == "incomplete"
        assert report["incomplete"] is True
        assert report["incomplete_reason"] == "token limit reached"
        assert "partial output" in report["raw_output"]

    def test_no_parsed_output(self) -> None:
        report = entrypoint.build_json_report(
            parsed=None,
            raw_output="garbage",
            model="opus",
            mode="build",
            service="x",
            duration=5.0,
        )
        assert report["overall"] == "error"
        assert report["scenarios_total"] == 0

    def test_date_field_present(self) -> None:
        parsed = {"scenarios": [{"id": 1, "status": "pass"}], "summary": "ok"}
        report = entrypoint.build_json_report(
            parsed=parsed,
            raw_output="",
            model="sonnet",
            mode="build",
            service="s",
            duration=1.0,
        )
        assert "date" in report
        assert "T" in report["date"]  # ISO format


# -- Markdown report building --


class TestBuildMarkdownReport:
    def test_includes_frontmatter(self) -> None:
        report = {
            "mode": "build",
            "service": "bid-scraper",
            "date": "2026-02-22T00:00:00Z",
            "model": "sonnet",
            "overall": "pass",
            "duration_seconds": 30.0,
            "scenarios_total": 2,
            "scenarios_pass": 2,
            "scenarios_fail": 0,
            "scenarios_error": 0,
            "scenarios": [],
            "summary": "All pass",
        }
        md = entrypoint.build_markdown_report(report)
        assert "auditor_mode: build" in md
        assert "service: bid-scraper" in md
        assert "overall: pass" in md

    def test_includes_scenario_details(self) -> None:
        report = {
            "mode": "prod",
            "service": "etl",
            "date": "2026-02-22",
            "model": "opus",
            "overall": "fail",
            "duration_seconds": 60.0,
            "scenarios_total": 1,
            "scenarios_pass": 0,
            "scenarios_fail": 1,
            "scenarios_error": 0,
            "scenarios": [
                {
                    "id": 1,
                    "status": "fail",
                    "description": "Data freshness",
                    "observation": "No new rows",
                    "evidence": "SELECT count=0",
                    "expected": "Daily records",
                }
            ],
            "summary": "Stale data",
        }
        md = entrypoint.build_markdown_report(report)
        assert "[FAIL]" in md
        assert "Data freshness" in md
        assert "No new rows" in md
        assert "SELECT count=0" in md

    def test_incomplete_report_shows_warning(self) -> None:
        report = {
            "mode": "build",
            "service": "test",
            "date": "2026-02-22",
            "model": "sonnet",
            "overall": "incomplete",
            "duration_seconds": 120.0,
            "scenarios_total": 0,
            "scenarios_pass": 0,
            "scenarios_fail": 0,
            "scenarios_error": 0,
            "scenarios": [],
            "summary": "",
            "incomplete": True,
            "incomplete_reason": "token limit reached",
        }
        md = entrypoint.build_markdown_report(report)
        assert "INCOMPLETE" in md
        assert "token limit reached" in md

    def test_raw_output_shown_on_parse_failure(self) -> None:
        report = {
            "mode": "build",
            "service": "x",
            "date": "2026-02-22",
            "model": "sonnet",
            "overall": "error",
            "duration_seconds": 5.0,
            "scenarios_total": 0,
            "scenarios_pass": 0,
            "scenarios_fail": 0,
            "scenarios_error": 0,
            "scenarios": [],
            "summary": "Parse failed",
            "raw_output": "Some garbage output from Claude",
        }
        md = entrypoint.build_markdown_report(report)
        assert "Raw Output" in md
        assert "Some garbage output" in md
