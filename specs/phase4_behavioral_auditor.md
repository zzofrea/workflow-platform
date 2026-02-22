# Phase 4: Behavioral Auditor Container

## Spec 1: Auditor produces a report from behavioral scenarios
; The auditor receives a spec and service access info, then validates each scenario.
GIVEN a behavioral spec with 3 GIVEN/WHEN/THEN scenarios and a service access document.
WHEN the auditor runs in build mode.
THEN a report file appears with a result for each scenario (pass, fail, or error).
AND each result includes an observation and evidence from the running service.
AND the report includes total token usage and wall-clock duration.

## Spec 2: Auditor has no access to source code
; The auditor validates as a client, never as a developer.
GIVEN the auditor container is running.
WHEN the auditor attempts to read project source code or implementation files.
THEN no source code is accessible inside the container.
AND only the spec document and access document are available as input.

## Spec 3: Auditor uses read-only database access
; The auditor can observe but never modify service data.
GIVEN the auditor has database connection details for a service.
WHEN the auditor queries the database to verify a scenario.
THEN SELECT queries succeed.
AND INSERT, UPDATE, DELETE, or DDL queries are denied.

## Spec 4: Token limit prevents runaway sessions
; Resource protection against unbounded AI usage.
GIVEN the auditor is configured with a token limit.
WHEN the audit session approaches the token limit before completing all scenarios.
THEN the report includes partial results for completed scenarios.
AND the report is flagged as incomplete with reason "token limit reached".
AND token usage in the report is at or near the configured limit.

## Spec 5: Auditor report routes through notifications
; Findings flow to the appropriate channels.
GIVEN the auditor completes a run and finds a failing scenario.
WHEN the report is processed.
THEN a notification fires via the notification hub with the finding summary.
AND the severity matches the failure type (critical for service-down, warning for data issues).

## Spec 6: Prod mode runs on schedule
; The auditor can be triggered as a recurring check.
GIVEN a service has a behavioral spec and runs in production.
WHEN the scheduled auditor run fires (e.g., daily after the service's own cron).
THEN the auditor validates all scenarios against the live prod service.
AND findings are routed through notifications.
AND the report is stored as an artifact.

## Spec 7: Report format is machine-parseable
; Reports must be consumable by both humans and downstream tooling.
GIVEN the auditor completes a run (pass or fail).
WHEN the report is written.
THEN a JSON report exists with fields: mode, service, date, model, overall status, token usage, duration, and per-scenario results.
AND a markdown report exists with the same information in human-readable form.
