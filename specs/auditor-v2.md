# Auditor v2: Direct-Access Behavioral Auditor

## System Overview

The behavioral auditor is a quality gate that verifies running services meet their
behavioral specifications. It runs as a single autonomous Claude instance inside a
Docker container with read-only database access (via psql) and bash/python tools,
acting like a human auditor who queries the data, checks it against defined
scenarios, and produces a pass/fail report. It replaces the existing 4-stage
pipeline (planner -> validator -> executor -> analyzer) with a 2-stage design
(host-side data bridge -> single auditor) that eliminates the fragile
AI-generated query plan handoff that kept producing "Query plan missing 'queries'
key" errors.

**Who it serves:** Zack (operator), triggered by `workflow-orchestrate` CLI after
service completion. Reports go to Discord via workflow-notify.

**Why it exists:** Autonomous services (bid scraper, ETL) need a synthetic user to
catch behavioral regressions without manual inspection. The previous 4-stage design
was over-engineered: it asked Claude to produce a strict JSON query plan, validated
that plan against an allowlist regex, executed the queries via ephemeral containers,
then asked a second Claude instance to analyze static result files. The planner
stage was the single point of failure -- Claude's output format compliance was
non-deterministic, and the entire audit failed whenever the JSON schema didn't
match exactly. Meanwhile, the planner's job was trivially deterministic: the
validator only accepted `SELECT * FROM table` against a fixed allowlist, so there
was nothing for Claude to "reason" about.

## Behavioral Contract

### Primary flows

- When the orchestrator triggers an audit for a service, the host-side bridge
  creates a temporary Docker network (non-internal, with outbound NAT for
  Anthropic API access), connects the target database container to it (with its
  hostname as a network alias), launches the auditor container on that single
  network, and the auditor produces a behavioral report.

- When the auditor starts, it reads the behavioral spec and access document from
  mounted files, connects to the database via psql using connection details from
  environment variables, runs whatever SELECT queries it needs to verify each
  scenario, and outputs a structured JSON report plus a human-readable markdown
  report.

- When the audit completes, the report is archived to
  `~/audit-reports/{service}/{mode}_{timestamp}/`, and a Discord notification is
  sent with the summary via workflow-notify.

### Error flows

- When the target database container is not running, the host-side bridge detects
  this before launching the auditor and fails with an error report. The operator
  is notified.

- When the auditor container exceeds its timeout (5 minutes default, 10 minutes
  max), the container is killed, an error report is generated, and the operator
  is notified.

- When Claude produces output that cannot be parsed as a valid report, the system
  generates an "incomplete" report containing the raw output for manual review.

### Boundary conditions

- When the temporary Docker network cannot be created or the target container
  cannot be connected, the audit fails before launching the auditor container.

- When the auditor container exits non-zero, the system still attempts to read
  any report files produced before generating an error report.

## Explicit Non-Behaviors

- The system must not use a multi-stage AI pipeline (planner/validator/executor/
  analyzer) because the previous design proved fragile and over-engineered for the
  actual use case. One Claude instance with direct read-only database access is
  sufficient.

- The system must not connect the auditor container to `dokploy-network` because
  that would expose all other services. Only the specific target DB is bridged
  onto the temporary audit network.

- The system must not use mounted files as the credential delivery mechanism.
  Credentials are delivered via standard libpq env vars (PGHOST, PGPORT, etc.).
  The access document is mounted for schema reference and happens to contain
  credentials, but the auditor's psql connection uses env vars, not file parsing.

- The system must not add retry logic for audit failures because the operator
  reviews failures manually and retries are handled by the next scheduled run.

- The system must not maintain the old 4-stage pipeline as a fallback because
  maintaining two code paths defeats the purpose of simplification.

- The system must not add curl/HTTP verification capabilities in this version.
  API endpoint reachability checks (bid-scraper spec scenario 2) are removed from
  the bid-scraper spec. The auditor verifies database state only.

- The system must not allow the auditor to run commands other than `psql`,
  `python3`, and `date` via Bash because unrestricted Bash access (curl, wget, nc)
  would allow data exfiltration over the network. The `--allowedTools` flag scopes
  Bash to `Bash(psql*),Bash(python3*),Bash(date*)` plus `Read`.

- The system must not add helpers, abstractions, or configuration layers beyond
  what is needed to replace the existing pipeline. No plugin system, no provider
  pattern, no generic "data source" abstraction.

- The system must not allow the auditor to perform INSERT, UPDATE, DELETE, or DDL
  operations. The `auditor_ro` Postgres role enforces SELECT-only at the database
  level, independent of any application-level controls.

## Integration Boundaries

### Target Database (via temporary Docker network)

- **Inbound to auditor:** psql CLI connection to target DB (read-only via
  `auditor_ro` role). Connection details passed as individual env vars:
  `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (standard libpq
  env vars that psql reads automatically).
- **Unavailability:** Host-side bridge checks `docker inspect` for running state
  before launching auditor. If the DB is down, fails immediately with error report.
  If connection drops mid-audit, Claude reports affected scenarios as "error."
- **Security:** Credentials via env vars only, never in mounted files. `auditor_ro`
  role is SELECT-only (enforced by Postgres). All secrets via env vars per
  constitution rule 17.

### Anthropic API (via temporary Docker network with outbound NAT)

- **Outbound from auditor:** Claude CLI calls Anthropic API for inference.
- **Format:** HTTPS to api.anthropic.com, handled by Claude CLI.
- **Network:** The temporary audit network is created as a standard (non-internal)
  Docker bridge network, which provides outbound NAT by default. This gives the
  auditor internet access for the Anthropic API without needing a second network.
- **Unavailability:** Claude CLI fails, auditor container exits non-zero,
  host-side code generates error report.
- **Security:** API key in Claude auth files, copied to writable home dir at
  container startup (existing pattern from current auditor, unchanged). Outbound
  exfiltration mitigated by scoped `--allowedTools` that restricts Bash to
  `psql`, `python3`, and `date` only -- no `curl`, `wget`, or `nc`.

### Docker Engine (host-side only)

- **Operations:** Create/remove temporary network, connect/disconnect containers,
  resolve container hostnames, run auditor container, inspect container state.
- **Security:** Host-side code runs Docker commands via subprocess. No Docker
  socket is mounted into the auditor container.

### workflow-notify (host-side only)

- **Outbound:** Fanout notification with service name, severity, summary,
  observation, evidence, and suggested action.
- **Format:** Python function call to `workflow_notify.fanout()`.
- **Unavailability:** Notification failure is logged but does not fail the audit.
  The report is still archived.

### Behavioral Specs (read-only mounted files)

- **Inbound to auditor:** `spec.md` mounted at `/audit/input/spec.md`.
- **Format:** Markdown with Given/When/Then scenarios (unchanged from current).

### Access Document (read-only mounted file)

- **Inbound to auditor:** `access.md` mounted at `/audit/input/access.md`.
  The full access document is mounted as-is, including table schemas, column
  definitions, key relationships, and data facts. Credentials are also present
  in the file but this is acceptable -- the security boundary is the `auditor_ro`
  Postgres role (SELECT-only), not credential concealment. The credentials are
  already available to the auditor via env vars.
- **Format:** Markdown with table/column documentation (unchanged from current
  access docs).

## Behavioral Scenarios

### Happy Path

#### Scenario 1: Successful audit produces a complete report

GIVEN a running target database with populated tables.
AND a behavioral spec with multiple scenarios.
WHEN the auditor runs against the service.
THEN a report.json file is produced with a result for each scenario in the spec.
AND each result has a status of "pass", "fail", or "error".
AND each result includes concrete evidence from database queries.
AND a report.md file is produced with the same content in human-readable format.
AND the report is archived to `~/audit-reports/{service}/{mode}_{timestamp}/`.
AND a Discord notification is sent with the overall result.

#### Scenario 2: Auditor queries the database directly

GIVEN the auditor has a live psql connection to the target database.
AND the spec requires verifying row counts, null checks, and date ranges.
WHEN the auditor processes each scenario.
THEN it runs SQL queries against the live database to gather evidence.
AND queries include SELECT with WHERE, COUNT, aggregations, and date comparisons
as needed -- not limited to `SELECT * FROM table`.
AND evidence in the report includes concrete values (counts, timestamps,
percentages).

#### Scenario 3: Temporary network is created and cleaned up

GIVEN the host-side bridge creates a single temporary Docker network with outbound
NAT (for Anthropic API access).
AND the target DB container is connected to it with its hostname as a network alias.
AND the auditor container is launched on that network.
WHEN the audit completes (whether success or failure).
THEN the auditor container is removed.
AND the target DB is disconnected from the temporary network.
AND the temporary network is deleted.
AND the target DB remains connected to dokploy-network throughout.

### Error Path

#### Scenario 4: Target database is unreachable

GIVEN the target database container is stopped.
WHEN the orchestrator triggers an audit.
THEN the host-side bridge detects the container is not running.
AND the audit fails before the auditor container is launched.
AND an error report is generated with a clear message about the unavailable DB.
AND the operator is notified via Discord.

#### Scenario 5: Auditor container times out

GIVEN the auditor container is running but has not completed within the timeout.
WHEN the timeout is reached.
THEN the container is killed.
AND an error report is generated with a timeout reason.
AND any temporary Docker network is cleaned up.
AND the operator is notified.

### Edge Cases

#### Scenario 6: Claude output is not parseable as JSON

GIVEN the auditor runs but Claude wraps its output in markdown fencing or includes
extra commentary around the JSON.
WHEN the report parser processes the raw output.
THEN it extracts the JSON from within the fencing or surrounding text.
AND if extraction fails entirely, the report is marked "incomplete" with the raw
output preserved for manual review.

#### Scenario 7: Database connection drops mid-audit

GIVEN the auditor has an active database connection.
AND the database container restarts during the audit.
WHEN the auditor tries to run a query for a scenario.
THEN that scenario is reported as "error" with the connection failure details.
AND the auditor continues to attempt remaining scenarios.
AND the final report reflects the partial results.

#### Scenario 8: Credentials use trust authentication (no password)

GIVEN a service whose database uses trust authentication (e.g., ETL postgres).
WHEN the connection environment variables are set.
THEN PGPASSWORD is either empty or unset.
AND the auditor connects successfully without a password.

#### Scenario 9: Report format is compatible with existing notification pipeline

GIVEN the auditor produces a report.
WHEN workflow-notify processes the report for Discord fanout.
THEN the report contains all fields expected by the notification system: overall,
service, mode, summary, scenarios (each with id, description, status, observation,
evidence, expected).
AND the notification renders identically to the previous auditor version.

## Definition of Done

- [ ] Acceptance tests pass (behavioral scenarios above, mocked Docker commands)
- [ ] Unit/integration tests pass (report parsing, bridge logic, env var construction)
- [ ] Old 4-stage code removed: planner/validator/executor functions in
      `two_stage_auditor.py`, planner/analyzer stage prompts in `entrypoint.py`,
      legacy stage handler in `entrypoint.py`
- [ ] Deployed to production: auditor image rebuilt, both services (bid-scraper
      and ETL) auditing successfully via existing crontab entries
- [ ] Monitoring in place: Discord notifications flowing for pass/fail/error
- [ ] Logging sufficient for debugging: structlog events for bridge creation,
      container launch, network cleanup, timeout, and all failure modes
- [ ] Bid-scraper spec updated: scenario 2 (API reachability) removed, remaining
      scenarios renumbered

## PROJECT.md

```markdown
# Auditor v2: Direct-Access Behavioral Auditor

## Objective

Replace the fragile 4-stage auditor pipeline (planner -> validator -> executor ->
analyzer) with a 2-stage design (host-side data bridge -> single auditor with
direct DB access). Eliminate the "Query plan missing 'queries' key" failure mode
by removing the planner stage entirely.

## Acceptance Criteria

- Auditor container connects to target DB via temporary Docker network
- Claude runs arbitrary SELECT queries to verify Given/When/Then scenarios
- Report format unchanged (report.json + report.md + Discord notification)
- Old planner/validator/executor code removed
- Both services (bid-scraper, ETL) auditing successfully in production
- Audit completes in under 5 minutes typical, 10 minutes max

## Constraints

- auditor_ro database role (SELECT-only) enforced at Postgres level
- Single temporary Docker network (non-internal, with outbound NAT) for isolation
- Credentials via env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE)
- Claude auth via copied ~/.claude.json (existing pattern)
- No Docker socket inside auditor container
- Scoped tools: Read, Bash(psql*), Bash(python3*), Bash(date*) -- no curl/wget/nc
- Max turns: 50, timeout: 5 min default / 10 min max

## What "Done" Means

- All acceptance and unit tests pass
- Old 4-stage pipeline code deleted (not commented out, not behind a flag)
- Production audits running for bid-scraper and ETL via existing crontab
- Discord notifications flowing for both services

## Out of Scope

- HTTP/API endpoint verification (curl-based checks)
- Retry logic for failed audits
- Changes to orchestrate CLI interface (build/deploy/monitor)
- Changes to workflow-notify integration
- Auditing the auditor
```

## Resolved Ambiguities

### 1. Bid-scraper spec scenario 2 (API endpoint reachability)

**Decision:** Remove scenario 2 from the bid-scraper spec entirely. The auditor
verifies database state only. API reachability can be revisited in a future version
if needed. Remaining scenarios are renumbered 1-9.

### 2. Network architecture: single temp network with outbound NAT

**Decision:** Use a single temporary Docker network (non-internal, default bridge
driver) which provides both database access (target DB connected with hostname
alias) and outbound internet (NAT for Anthropic API). No dual-network needed.
Launch auditor with `--network audit-{service}-{uuid}`. Simpler than the
dual-network alternative and achieves the same isolation -- auditor cannot reach
dokploy-network, only the specific target DB.

### 3. Access document: mount full doc as-is

**Decision:** Mount the full access document (including credentials and table
schemas) as `/audit/input/access.md`. The security boundary is the `auditor_ro`
Postgres role (SELECT-only), not credential concealment. The credentials are
already available via env vars. No need to maintain separate stripped files.

### 4. Max turns: 50

**Decision:** Set `--max-turns 50`. With 10 scenarios x 2-3 queries each plus
spec reading and report generation, 50 turns provides comfortable headroom.
The 5-minute timeout is the real safety valve against runaway sessions.

### 5. System prompt: concise, high-level, let Claude figure it out

**Decision:** System prompt establishes identity and output format, not query
strategy. Claude decides how to verify each scenario. Prompt:

> You are a behavioral auditor. You verify that a running service meets its
> specification by querying its database and checking the results. You act like
> a user or downstream consumer of this service -- you check observable outcomes,
> not implementation details.
>
> You have psql access to the service database (connection details are in your
> environment variables). Read the spec, run queries to verify each scenario,
> and produce a JSON report.

Followed by the JSON output format definition. No prescriptive query examples.

### 6. Tool scoping for exfiltration prevention

**Decision:** Restrict `--allowedTools` to
`Read,Bash(psql*),Bash(python3*),Bash(date*)`. This prevents the auditor from
running `curl`, `wget`, `nc`, or any other command that could exfiltrate data
over the network. The auditor can only run psql (database queries), python3
(data analysis), and date (timestamp checks). This is the same scoped-tools
pattern already used in the current entrypoint.py.

## Remaining Ambiguity Warnings

No unresolved ambiguities. All design decisions have been made.

## Implementation Constraints

- **Language:** Python 3.12+ (matches existing codebase)
- **Test runner:** pytest
- **Logging:** structlog
- **Docker image:** Based on existing Dockerfile.auditor (node:20-slim with
  python3, postgresql-client, curl, Claude CLI)
- **CLI interface:** No changes to `workflow-orchestrate` CLI -- `run_two_stage_audit()`
  is replaced by a new `run_audit()` function with the same call signature
- **Linting:** ruff check + ruff format + pyright (existing CI)

## Constitution Compliance

| Rule | How Satisfied |
|------|---------------|
| 1-6 (Specs before code, Given/When/Then) | 9 behavioral scenarios in this spec, plain domain language, external observables only |
| 7 (Human approval before implementation) | This spec requires user approval via /spec workflow |
| 8-10 (Two test streams, red-green, pytest) | Definition of Done requires both acceptance and unit/integration tests, pytest runner |
| 11 (Type hints, docstrings, PEP 8) | Implementation follows existing codebase style, enforced by ruff + pyright |
| 12 (Simple functions over classes) | Existing module uses functions, new code follows same pattern |
| 13-14 (Error handling, no shortcuts) | Error flows specified: DB unavailable, timeout, parse failure, mid-audit disconnect |
| 15-16 (Modular, standardized patterns) | Same container pattern, same report format, same notification flow as current system |
| 17 (Env vars for secrets) | PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE via env vars. Access doc mounted but credentials also in env vars; security boundary is DB role, not file access |
| 18 (Validate at boundaries) | Host-side bridge validates DB container is running before launching auditor |
| 19 (No injection) | Connection details via standard libpq env vars (not string interpolation). auditor_ro is SELECT-only at DB level. Scoped --allowedTools prevents exfiltration (no curl/wget/nc) |
| 20-21 (Docker + Dokploy, unique hostnames) | Auditor runs as Docker container on temp network with hostname aliases |
| 22 (PROJECT.md) | Included above |
| 23-26 (Definition of Done) | Acceptance tests, deployment, monitoring, logging all specified |
| 27-30 (Simplicity) | Entire spec is about removing complexity. Deletes ~400 lines of planner/validator/executor code. No new abstractions |
