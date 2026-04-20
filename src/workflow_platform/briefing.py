"""Briefing orchestration: gather -> synthesize (workflow-agent) -> post.

This module coordinates the three phases of a daily briefing run:

1. gather  — docker exec daily-briefing-agent briefing gather {mode}
             Outputs JSON context to stdout.

2. synthesize — writes the context as a markdown spec file into the
                workflow-agent agents/daily-briefing/specs/ directory,
                then calls _run_workflow_agent to invoke Claude synthesis.
                Returns the synthesized briefing text from report["content"].

3. post    — docker exec -i daily-briefing-agent briefing post {mode}
             Reads briefing text from stdin, posts to Discord + Open Brain.

Modes: morning | consolidate | weekly
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from workflow_platform.orchestrate import _run_workflow_agent

log = structlog.get_logger("workflow_platform.briefing")

_ET = ZoneInfo("America/New_York")

BRIEFING_CONTAINER = "daily-briefing-agent"
CONTEXT_SPEC_DIR = Path.home() / "workflow-agent" / "agents" / "daily-briefing" / "specs"

# Per-mode synthesis timeouts — weekly has 2x context of morning/consolidate
SYNTHESIS_TIMEOUTS: dict[str, int] = {
    "morning": 120,
    "consolidate": 120,
    "weekly": 300,
}


def _gather(mode: str) -> dict[str, Any] | None:
    """Run gather inside the briefing container, return parsed JSON."""
    log.info("briefing.gather_start", mode=mode)
    try:
        result = subprocess.run(
            ["docker", "exec", BRIEFING_CONTAINER, "briefing", "gather", mode],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.error(
                "briefing.gather_failed",
                mode=mode,
                exit_code=result.returncode,
                stderr=result.stderr[:500],
            )
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.error("briefing.gather_timeout", mode=mode)
        return None
    except json.JSONDecodeError as exc:
        log.error("briefing.gather_invalid_json", mode=mode, error=str(exc))
        return None
    except Exception as exc:
        log.error("briefing.gather_error", mode=mode, error=str(exc))
        return None


def _render_context(mode: str, context: dict[str, Any]) -> str:
    """Render gathered JSON context as a human-readable markdown document for Claude."""
    lines: list[str] = [
        f"# Briefing Context — {mode.capitalize()}",
        f"Generated: {context.get('as_of', datetime.now(UTC).isoformat())}",
        f"Date: {context.get('date', datetime.now(_ET).date().isoformat())}",
        "",
    ]

    # Surface Google auth failures prominently so they appear in the posted briefing
    google_errors = context.get("google_errors", [])
    if google_errors:
        lines.append("## DATA WARNING")
        lines.append(
            "**Google auth failed — calendar and email data is MISSING from this briefing.**"
        )
        lines.append("Refresh tokens need to be regenerated in Google Cloud Console.")
        lines.append("")

    # Calendar
    calendar = context.get("calendar", [])
    if calendar:
        lines.append("## Calendar")
        for ev in calendar:
            start = ev.get("start", "")
            end = ev.get("end", "")
            summary = ev.get("summary", "(no title)")
            location = ev.get("location", "")
            cal_name = ev.get("calendar", "")
            loc_str = f" @ {location}" if location else ""
            cal_str = f" [{cal_name}]" if cal_name else ""
            lines.append(f"- {start} – {end}: **{summary}**{loc_str}{cal_str}")
        lines.append("")

    # Emails (last 24h, read and unread)
    emails = context.get("emails", [])
    if emails:
        lines.append("## Recent Emails (last 24h)")
        for msg in emails:
            unread_marker = "[UNREAD] " if msg.get("unread") else ""
            lines.append(f"- **{unread_marker}{msg.get('subject', '(no subject)')}**")
            lines.append(f"  From: {msg.get('from', '')} | {msg.get('date', '')}")
            snippet = msg.get("snippet", "")
            if snippet:
                lines.append(f"  {snippet}")
        lines.append("")

    # Open Issues
    issues = context.get("open_issues", [])
    if issues:
        lines.append("## Open Issues")
        for issue in issues:
            sev = issue.get("severity", "").upper()
            status = issue.get("status", "")
            title = issue.get("title", "")
            opened = issue.get("opened_date", "")
            lines.append(f"- [{sev}/{status}] **{title}** (opened {opened})")
            if issue.get("description"):
                lines.append(f"  {issue['description']}")
            if issue.get("blocking"):
                lines.append(f"  Blocking: {issue['blocking']}")
        lines.append("")

    # Upcoming Maintenance
    maintenance = context.get("upcoming_maintenance", [])
    if maintenance:
        lines.append("## Upcoming Maintenance")
        for item in maintenance:
            due = item.get("follow_up_date", "")
            asset = item.get("asset_name", "general")
            notes = item.get("follow_up_notes") or item.get("summary", "")
            lines.append(f"- {due}: {asset} — {notes}")
        lines.append("")

    # Recent Thoughts / Today's Thoughts / Weekly Thoughts
    for key, label in [
        ("recent_thoughts", "Recent Captures"),
        ("todays_thoughts", "Today's Captures"),
        ("weekly_thoughts", "This Week's Captures"),
    ]:
        thoughts = context.get(key, [])
        if thoughts:
            lines.append(f"## {label}")
            for t in thoughts:
                ts = str(t.get("created_at", ""))[:16]
                content = t.get("raw_content", "")
                lines.append(f"- [{ts}] {content}")
            lines.append("")

    # Morning Briefing (for consolidate)
    morning = context.get("morning_briefing", [])
    if morning:
        lines.append("## Morning Briefing")
        for t in morning:
            lines.append(t.get("raw_content", "").strip())
        lines.append("")

    # Daily Summaries (for weekly)
    summaries = context.get("daily_summaries", [])
    if summaries:
        lines.append("## Daily Summaries (This Week)")
        for t in summaries:
            ts = str(t.get("created_at", ""))[:10]
            lines.append(f"### {ts}")
            lines.append(t.get("raw_content", "").strip())
            lines.append("")

    # Reminders & Backlog (from upcoming_reminders extension)
    reminders = context.get("extensions", {}).get("upcoming_reminders.items", [])
    if reminders:
        lines.append("## Reminders & Backlog")
        for r in reminders:
            title = r.get("title", "")
            priority = (r.get("priority") or "medium").upper()
            deadline = r.get("deadline_date")
            deadline_time = r.get("deadline_time")
            notes = r.get("notes", "")
            if deadline:
                due = f"due: {deadline}"
                if deadline_time:
                    due += f" {deadline_time}"
            else:
                due = "backlog"
            note_str = f" — {notes}" if notes else ""
            lines.append(f"- [{priority}] {title} ({due}){note_str}")
        lines.append("")

    # Prior Observations (loop-agent write-backs for cross-run continuity)
    prior_obs = context.get("extensions", {}).get("prior_observations.items", [])
    if prior_obs:
        lines.append("## Prior Observations")
        for item in prior_obs:
            date_str = str(item.get("created_at", "unknown"))[:10]
            lines.append(f"### {date_str}")
            lines.append(item.get("raw_content", "").strip())
            lines.append("")

    # Other extensions (generic fallback for any future extensions)
    extensions = context.get("extensions", {})
    _RENDERED_EXT_KEYS = {"upcoming_reminders.items", "prior_observations.items"}
    for ext_key, rows in extensions.items():
        if ext_key in _RENDERED_EXT_KEYS:
            continue  # already rendered above
        if rows:
            lines.append(f"## Extension: {ext_key}")
            for row in rows:
                lines.append(f"- {row}")
            lines.append("")

    return "\n".join(lines)


def _write_context_spec(mode: str, markdown: str) -> None:
    """Write the context markdown to the workflow-agent spec file for this mode."""
    CONTEXT_SPEC_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = CONTEXT_SPEC_DIR / f"context-{mode}.md"
    spec_path.write_text(markdown)
    log.info("briefing.context_written", mode=mode, path=str(spec_path), bytes=len(markdown))


def _synthesize(mode: str) -> str | None:
    """Run workflow-agent synthesis for the given mode.

    Returns the synthesized briefing text, or None on failure.
    """
    log.info("briefing.synthesize_start", mode=mode)
    report, run_id = _run_workflow_agent(
        service="daily-briefing",
        role=mode,
        model="sonnet",
        max_turns=10,
        timeout=SYNTHESIS_TIMEOUTS.get(mode, 120),
        no_notify=True,  # briefing handles its own notifications
    )

    content = report.get("content", "").strip()
    if not content:
        log.error("briefing.synthesize_empty", mode=mode, run_id=run_id)
        return None

    log.info("briefing.synthesize_complete", mode=mode, run_id=run_id, length=len(content))
    return content


def _post(mode: str, briefing_text: str) -> bool:
    """Post briefing text to Discord + Open Brain via the briefing container."""
    log.info("briefing.post_start", mode=mode, length=len(briefing_text))
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", BRIEFING_CONTAINER, "briefing", "post", mode],
            input=briefing_text,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.error(
                "briefing.post_failed",
                mode=mode,
                exit_code=result.returncode,
                stderr=result.stderr[:500],
            )
            return False
        log.info("briefing.post_complete", mode=mode)
        return True
    except subprocess.TimeoutExpired:
        log.error("briefing.post_timeout", mode=mode)
        return False
    except Exception as exc:
        log.error("briefing.post_error", mode=mode, error=str(exc))
        return False


def _writeback(mode: str, briefing_text: str) -> None:
    """Capture the briefing synthesis into Open Brain as a loop-agent thought.

    Fire-and-forget: must be called AFTER _post() completes so that a capture
    failure never blocks delivery. Failures are logged but never propagated.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", BRIEFING_CONTAINER, "briefing", "writeback", mode],
            input=briefing_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                "briefing.writeback_failed",
                mode=mode,
                exit_code=result.returncode,
                stderr=result.stderr[:300],
            )
        else:
            log.info("briefing.writeback_ok", mode=mode)
    except subprocess.TimeoutExpired:
        log.warning("briefing.writeback_timeout", mode=mode)
    except Exception as exc:
        log.warning("briefing.writeback_error", mode=mode, error=str(exc))


def _notify_failure(mode: str, stage: str, detail: str) -> None:
    """Send a workflow-notify warning for a briefing failure."""
    try:
        from workflow_notify import NotifyConfig, fanout

        fanout(
            config=NotifyConfig(),
            service="daily-briefing",
            severity="warning",
            message=f"Briefing {mode} FAILED at {stage}: {detail}",
        )
    except ImportError:
        log.warning("briefing.notify_unavailable")
    except Exception as exc:
        log.warning("briefing.notify_failed", error=str(exc))


def cmd_briefing(mode: str) -> bool:
    """Run a full briefing cycle: gather -> synthesize -> post.

    Returns True if all three phases succeeded.
    Each phase degrades gracefully: a failure is notified and returns False,
    but does not raise.
    """
    log.info("briefing.start", mode=mode)
    print(f"=== Briefing: {mode} ===")

    # Phase 1: Gather
    print("\n--- Phase 1: Gather ---")
    context = _gather(mode)
    if context is None:
        _notify_failure(mode, "gather", "gather returned no context")
        return False
    print(f"Gathered context ({len(str(context))} chars)")

    # Alert immediately if Google auth has failed — don't let it be silent
    google_errors = context.get("google_errors", [])
    if google_errors:
        _notify_failure(
            mode,
            "google_auth",
            "Google OAuth tokens invalid (invalid_grant). Calendar and email missing. "
            "Regenerate GOOGLE_REFRESH_TOKEN_1 and GOOGLE_REFRESH_TOKEN_2 in Dokploy.",
        )

    # Phase 2: Render + synthesize
    print("\n--- Phase 2: Synthesize ---")
    markdown = _render_context(mode, context)
    _write_context_spec(mode, markdown)

    briefing_text = _synthesize(mode)
    if briefing_text is None:
        _notify_failure(mode, "synthesize", "workflow-agent returned empty content")
        return False
    print(f"Synthesized briefing ({len(briefing_text)} chars)")

    # Phase 3: Post
    print("\n--- Phase 3: Post ---")
    ok = _post(mode, briefing_text)
    if not ok:
        _notify_failure(mode, "post", "docker exec post returned non-zero")
        return False

    # Phase 4: Loop write-back (fire-and-forget — must not block delivery)
    _writeback(mode, briefing_text)

    print(f"Briefing {mode} complete.")
    log.info("briefing.complete", mode=mode)
    return True
