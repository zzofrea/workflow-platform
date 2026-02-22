#!/usr/bin/env python3
"""Behavioral auditor entrypoint -- runs inside the container.

Reads spec + access docs from /audit/input/, invokes Claude CLI with
constrained tools, parses the response into report.json and report.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

INPUT_DIR = "/audit/input"
OUTPUT_DIR = "/audit/output"

SYSTEM_PROMPT = """\
You are a behavioral auditor. Your job is to verify that a running service \
satisfies its behavioral specification. You are a CLIENT auditor -- you test \
from the outside, like a user or downstream system would.

Rules:
1. You receive a behavioral spec (GIVEN/WHEN/THEN scenarios) and an access \
document describing how to reach the service (DB connection strings, API \
endpoints, etc.).
2. For each scenario, design your own verification approach. Query databases, \
call APIs, or inspect endpoints as described in the access document.
3. Report ONLY observable findings with concrete evidence (actual query \
results, HTTP responses, row counts, timestamps).
4. NEVER modify data. Use SELECT queries only. Do not INSERT, UPDATE, DELETE, \
or run DDL.
5. NEVER access source code. You validate behavior, not implementation.
6. If a scenario cannot be verified (e.g., access denied, service unreachable), \
report it as "error" with the reason.

Output format -- respond with ONLY a JSON object (no markdown fencing, no \
extra text):
{
  "scenarios": [
    {
      "id": 1,
      "description": "Brief description of the scenario",
      "status": "pass" | "fail" | "error",
      "observation": "What you actually observed",
      "evidence": "Concrete data: query results, HTTP responses, etc.",
      "expected": "What the spec says should happen"
    }
  ],
  "summary": "One-line overall assessment"
}
"""


def read_input_file(name: str) -> str:
    """Read a file from the input directory."""
    path = os.path.join(INPUT_DIR, name)
    if not os.path.exists(path):
        print(f"Warning: {path} not found", file=sys.stderr)
        return ""
    with open(path) as f:
        return f.read()


def build_prompt(spec: str, access: str) -> str:
    """Combine spec and access docs into the audit prompt."""
    parts = ["Verify the following behavioral specification against the running service.\n"]
    parts.append("## Behavioral Specification\n")
    parts.append(spec)
    parts.append("\n## Service Access\n")
    parts.append(access)
    parts.append(
        "\n\nVerify EACH scenario by querying the service. "
        "Respond with the JSON report as described in your instructions."
    )
    return "\n".join(parts)


def run_claude(prompt: str, model: str, max_turns: int) -> tuple[str, float]:
    """Invoke Claude CLI and return (output_text, duration_seconds)."""
    cmd = [
        "claude",
        "--print",
        "--model",
        model,
        "--output-format",
        "text",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--allowedTools",
        "Bash(psql:*),Bash(curl:*),Bash(date),Read",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
    ]

    if max_turns > 0:
        cmd.extend(["--max-turns", str(max_turns)])

    start = time.monotonic()
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute hard timeout
    )
    duration = time.monotonic() - start

    if result.returncode != 0:
        print(f"Claude CLI stderr: {result.stderr}", file=sys.stderr)

    return result.stdout, duration


def parse_report(raw_output: str) -> dict | None:
    """Extract the JSON report from Claude's output.

    Claude might wrap the JSON in markdown fencing or include extra text.
    We try to find the JSON object.
    """
    # Try direct parse first
    text = raw_output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in markdown fencing
    for marker in ["```json", "```"]:
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    # Try to find a JSON object by braces
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


def build_json_report(
    parsed: dict | None,
    raw_output: str,
    model: str,
    mode: str,
    service: str,
    duration: float,
    incomplete: bool = False,
    incomplete_reason: str = "",
) -> dict:
    """Build the final structured report."""
    now = datetime.now(UTC).isoformat()

    scenarios = []
    overall = "error"

    if parsed and "scenarios" in parsed:
        scenarios = parsed["scenarios"]
        statuses = [s.get("status", "error") for s in scenarios]
        if all(s == "pass" for s in statuses):
            overall = "pass"
        elif any(s == "fail" for s in statuses):
            overall = "fail"
        else:
            overall = "partial"

    if incomplete:
        overall = "incomplete"

    return {
        "mode": mode,
        "service": service,
        "date": now,
        "model": model,
        "overall": overall,
        "duration_seconds": round(duration, 1),
        "scenarios_total": len(scenarios),
        "scenarios_pass": sum(1 for s in scenarios if s.get("status") == "pass"),
        "scenarios_fail": sum(1 for s in scenarios if s.get("status") == "fail"),
        "scenarios_error": sum(1 for s in scenarios if s.get("status") not in ("pass", "fail")),
        "incomplete": incomplete,
        "incomplete_reason": incomplete_reason,
        "scenarios": scenarios,
        "summary": parsed.get("summary", "") if parsed else "Failed to parse auditor output",
        "raw_output": raw_output[:5000] if not parsed else "",
    }


def build_markdown_report(report: dict) -> str:
    """Render the JSON report as human-readable markdown."""
    lines = [
        "---",
        f"auditor_mode: {report['mode']}",
        f"service: {report['service']}",
        f"date: {report['date']}",
        f"model: {report['model']}",
        f"overall: {report['overall']}",
        f"duration_seconds: {report['duration_seconds']}",
        f"scenarios_total: {report['scenarios_total']}",
        f"scenarios_pass: {report['scenarios_pass']}",
        f"scenarios_fail: {report['scenarios_fail']}",
        f"scenarios_error: {report['scenarios_error']}",
        "---",
        "",
        f"# Audit Report: {report['service']}",
        "",
        f"**Mode:** {report['mode']}  ",
        f"**Date:** {report['date']}  ",
        f"**Model:** {report['model']}  ",
        f"**Overall:** {report['overall']}  ",
        f"**Duration:** {report['duration_seconds']}s  ",
        "",
    ]

    if report.get("incomplete"):
        lines.append(f"> **INCOMPLETE:** {report['incomplete_reason']}")
        lines.append("")

    if report.get("summary"):
        lines.append(f"**Summary:** {report['summary']}")
        lines.append("")

    lines.append("## Scenarios")
    lines.append("")

    for s in report.get("scenarios", []):
        status_icon = {"pass": "[PASS]", "fail": "[FAIL]", "error": "[ERROR]"}.get(
            s.get("status", "error"), "[???]"
        )
        desc = s.get("description", "N/A")
        lines.append(f"### {status_icon} Scenario {s.get('id', '?')}: {desc}")
        lines.append("")
        lines.append(f"**Expected:** {s.get('expected', 'N/A')}")
        lines.append("")
        lines.append(f"**Observation:** {s.get('observation', 'N/A')}")
        lines.append("")
        if s.get("evidence"):
            lines.append("**Evidence:**")
            lines.append("```")
            lines.append(s["evidence"])
            lines.append("```")
            lines.append("")

    if report.get("raw_output"):
        lines.append("## Raw Output (parse failed)")
        lines.append("```")
        lines.append(report["raw_output"])
        lines.append("```")

    return "\n".join(lines)


def main() -> None:
    """Entrypoint: read inputs, run audit, write outputs."""
    mode = os.environ.get("AUDITOR_MODE", "build")
    model = os.environ.get("AUDITOR_MODEL", "sonnet")
    service = os.environ.get("AUDITOR_SERVICE", "unknown")
    max_turns = int(os.environ.get("AUDITOR_MAX_TURNS", "20"))

    print(f"Auditor starting: mode={mode} model={model} service={service}")

    spec = read_input_file("spec.md")
    access = read_input_file("access.md")

    if not spec:
        print("Error: No spec.md found in /audit/input/", file=sys.stderr)
        sys.exit(1)

    prompt = build_prompt(spec, access)

    # Run the audit
    raw_output, duration = run_claude(prompt, model, max_turns)

    print(f"Audit completed in {duration:.1f}s")

    # Check for truncation / incomplete signals
    incomplete = False
    incomplete_reason = ""
    if not raw_output.strip():
        incomplete = True
        incomplete_reason = "Empty response from Claude CLI"

    # Parse and build report
    parsed = parse_report(raw_output)
    if parsed is None and raw_output.strip():
        incomplete = True
        incomplete_reason = "Could not parse structured report from output"

    report = build_json_report(
        parsed=parsed,
        raw_output=raw_output,
        model=model,
        mode=mode,
        service=service,
        duration=duration,
        incomplete=incomplete,
        incomplete_reason=incomplete_reason,
    )

    md_report = build_markdown_report(report)

    # Write outputs
    with open(os.path.join(OUTPUT_DIR, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    with open(os.path.join(OUTPUT_DIR, "report.md"), "w") as f:
        f.write(md_report)

    print(f"Reports written to {OUTPUT_DIR}/")
    print(f"Overall: {report['overall']}")

    # Exit with non-zero if any failures
    if report["overall"] in ("fail", "error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
