# Workflow Platform Specification

## System Overview

A disciplined development lifecycle platform that sits on top of Dokploy, formalizing how projects go from idea to production and stay healthy once deployed. It provides dev/prod environment isolation, an AI-driven build-validate cycle with independent behavioral auditing, automated prod monitoring with issue tracking, and human approval gates at every irreversible decision point. The platform serves a solo developer (Zack) working with Claude Code as the primary AI agent.

## Behavioral Contract

### Spec Phase
- When a user starts a new project or feature, the system provides a structured brainstorming workflow that produces a behavioral specification document with GIVEN/WHEN/THEN scenarios.
- When a spec is approved, it is stored in a known location within the project repo and becomes the contract for both the build and validation phases.

### Dev Environment Lifecycle
- When a new project is initiated, the system provisions an isolated dev environment in Dokploy with its own containers, database(s), and network — fully separated from prod.
- When a dev environment is not in active use, it can be spun down to conserve resources.
- When a spun-down dev environment is needed again, it can be restored to its previous state (code, data, config).
- When a dev environment exists, it mirrors the prod environment's structure (same Dockerfile, same compose shape, same env var keys) but with dev-specific values.

### Build Phase
- When a spec is approved, Claude Code executes the implementation plan in the dev environment.
- When the build phase completes, all internal tests (unit, integration) pass before proceeding.
- When internal tests pass, the system hands off to the behavioral auditor — not the other way around.

### Validation Phase (Behavioral Auditor — Build Mode)
- When a build is ready for validation, a separate AI agent receives ONLY the behavioral spec (GIVEN/WHEN/THEN scenarios) and access to the running service's external interfaces (database queries, API calls, UI interactions — whatever a client would use).
- When the auditor receives the spec, it independently designs its own verification approach — it does not receive or execute developer-written tests.
- When the auditor finds all scenarios satisfied, it reports success.
- When the auditor finds failures, it returns specific, observable findings (not code fixes) back to the build agent for iteration.
- When the build-validate cycle iterates, the auditor re-runs its full verification suite, not just the previously failing checks.

### Human Review Gate
- When the auditor reports success, the human reviews the build, the spec outcomes, and the auditor's report.
- When the human approves, the project proceeds to deployment.
- When the human requests changes, the build-validate cycle restarts with updated requirements.

### Deployment (Dev to Prod)
- When a project is approved for prod, deployment happens via git push to the main branch — Dokploy auto-pulls and redeploys.
- When deploying, environment variables and secrets for prod are configured in Dokploy, never in code or git.
- When deployment completes, the system sends a confirmation notification (Discord and/or email).
- When a dev environment is no longer needed post-deployment, it can be spun down.

### Monitoring Phase (Behavioral Auditor — Prod Mode)
- When a service is running in prod, the behavioral auditor runs on a scheduled cadence (e.g., daily) using the same GIVEN/WHEN/THEN spec to verify the service still behaves correctly.
- When the auditor detects behavioral drift (stale data, missing records, schema changes, silent failures), it creates a task in the Obsidian vault backlog with a clear description of what's wrong.
- When a critical failure is detected (service down, data pipeline stopped, zero new records when expected), an alert is sent immediately via Discord and email.
- When a non-critical issue is detected (degraded performance, minor data quality issues), it is logged to the Obsidian backlog without an immediate alert.
- When a scheduled process runs (scrapers, ETL jobs, cron tasks), a notification is sent confirming execution and basic outcome (records processed, errors encountered).

### Catchup / Recovery
- When a process fails or is interrupted (reboot, network outage, API downtime), it resumes from where it left off on the next run rather than skipping the missed window.
- When the platform itself restarts (Beelink reboot), all services and scheduled tasks resume automatically without manual intervention.
- When an external dependency is unavailable, the system retries with backoff and logs the outage, rather than silently succeeding with no data.

### Paper Trail
- When any change is made to code, it is captured in a git commit with a conventional commit message.
- When any deployment happens, it is logged with timestamp, commit hash, and who/what triggered it.
- When the auditor runs (build or prod mode), its findings are stored as a report artifact.
- When the monitoring agent files an issue, it includes the auditor report, relevant log excerpts, and a suggested action.

## Explicit Non-Behaviors

- The system must NOT deploy to prod without explicit human approval, because silent prod changes are the highest-risk failure mode.
- The system must NOT modify application logic in prod, because all code changes flow through the dev→validate→approve→deploy pipeline.
- The system must NOT swallow exceptions or errors silently, because silent failures are the primary threat this system exists to prevent. Every error must be logged, and unexpected errors must be surfaced.
- The system must NOT give the behavioral auditor access to source code or implementation details, because the auditor's value comes from testing like a client, not like a developer.
- The system must NOT auto-fix issues found in prod monitoring, because fixes require going through the full dev→validate→approve→deploy cycle.
- The system must NOT store secrets in git, config files, or anywhere outside Dokploy's environment variable management, because credential leakage is an unacceptable security risk.
- The system must NOT require the human to manually restart services after a system reboot, because the platform should be self-healing at the infrastructure level.

## Integration Boundaries

### Dokploy (Container Runtime)
- **In:** API calls to create/manage projects, environments, applications, compose stacks, deployments, and scheduled tasks.
- **Out:** Container status, deployment logs, application endpoints.
- **Contract:** REST API with `x-api-key` auth. MCP tools available for Claude Code.
- **Unavailable:** Queue commands and retry when Dokploy API is unreachable. Alert if down for more than 15 minutes.

### GitHub (Code & Deployment)
- **In:** Git push triggers auto-deploy in Dokploy. Repo hosts all project code, Dockerfiles, compose files.
- **Out:** Commit history, branch state, CI status.
- **Contract:** Git over SSH, GitHub CLI for PR/issue operations.
- **Unavailable:** Local development continues; deployment blocked until GitHub is reachable. Alert on prolonged outage.

### Obsidian Vault (Backlog & Issue Tracking)
- **In:** Monitoring agent writes markdown files to the vault's inbox folder.
- **Out:** Human reads and triages issues at their own pace.
- **Contract:** Markdown files in `/opt/vault/second-brain/` following a consistent template. Git-backed (auto-committed every 30 min by existing cron).
- **Unavailable:** Issues buffered locally and written when vault is accessible.

### Discord (Alerts & Notifications)
- **In:** Webhook calls for alerts, execution confirmations, and status updates.
- **Out:** None (one-way notification).
- **Contract:** Discord webhook URL, markdown-formatted messages.
- **Unavailable:** Fall back to email. Log the notification for retry.

### Email (Alerts & Notifications)
- **In:** SMTP calls for critical alerts and execution summaries.
- **Out:** None (one-way notification).
- **Contract:** Gmail SMTP with app-specific credentials stored in Dokploy env vars.
- **Unavailable:** Log the alert. Discord serves as secondary channel.

### n8n (Workflow Glue)
- **In:** Webhook triggers, scheduled triggers, API calls from other services.
- **Out:** Orchestrated calls to Discord webhooks, email, Obsidian vault writes, and external APIs.
- **Contract:** n8n workflow definitions, HTTP webhook endpoints.
- **Unavailable:** Scheduled tasks missed; catchup on restart. Alert via direct email/Discord if possible.

### Claude Code (Primary AI Interface)
- **In:** Human instructions, spec documents, build plans, auditor reports.
- **Out:** Code, commits, Dokploy API calls, deployment actions.
- **Contract:** CLI tool invoked over SSH. Reads/writes project files. Calls Dokploy MCP tools.
- **Unavailable:** Human works manually or waits. No automated fallback needed — this is the interactive interface.

## Behavioral Scenarios

### Happy Path

**Scenario 1: New project from idea to prod**
```
GIVEN I have an idea for a new data scraping service.
WHEN I open Claude Code and run /spec to brainstorm, iterate, and approve a behavioral specification.
AND Claude Code builds the project in a dev environment.
AND the behavioral auditor independently confirms all spec scenarios pass against the running dev service.
AND I review the build and approve it for production.
AND I trigger deployment to prod.
THEN the service is running in prod, accessible to clients, with monitoring active and notifications flowing.
```

**Scenario 2: Prod monitoring catches stale data**
```
GIVEN a scraper service is running in prod with a spec that says "new records appear daily."
WHEN the behavioral auditor runs its daily check and finds no new records in the last 48 hours.
THEN a task appears in my Obsidian vault backlog describing the staleness.
AND I receive a Discord alert and email with the finding.
AND the alert includes when the last successful ingestion occurred and what the auditor observed.
```

**Scenario 3: Dev environment spin-down and restore**
```
GIVEN a project was built and deployed to prod, and the dev environment was spun down.
WHEN I need to add a feature and request the dev environment be restored.
THEN the dev environment comes back up with the same code, config shape, and database schema as prod.
AND I can begin a new build-validate cycle without manual setup.
```

### Error Scenarios

**Scenario 4: Build fails validation**
```
GIVEN Claude Code has completed a build and internal tests pass.
WHEN the behavioral auditor checks the spec scenarios and finds that querying the database returns records with missing required fields.
THEN the auditor reports the specific finding: "Records exist but field X is null in 30% of rows, spec requires it to be populated."
AND Claude Code receives this finding and iterates on the implementation.
AND the auditor re-runs ALL scenarios (not just the failed one) on the next attempt.
```

**Scenario 5: External dependency outage during scheduled run**
```
GIVEN a scraper is scheduled to run daily at 6 AM.
WHEN the target website is unreachable at 6 AM.
THEN the scraper logs the failure, retries with backoff, and ultimately records that the run failed.
AND a notification is sent indicating the run failed due to the target being unavailable.
AND on the next scheduled run, the scraper processes both the missed day and the current day (catchup behavior).
```

### Edge Cases

**Scenario 6: Auditor and developer disagree**
```
GIVEN the behavioral auditor reports a scenario as failing.
WHEN the developer believes the auditor's interpretation is incorrect (the spec is ambiguous).
THEN the human reviews both the auditor's report and the running service.
AND the human either updates the spec to be more precise or directs the developer to fix the implementation.
AND the decision is documented (commit message or spec amendment).
```

**Scenario 7: Multiple dev projects running simultaneously**
```
GIVEN two projects are in active development, each with their own dev environment.
WHEN both dev environments are running on the Beelink simultaneously.
THEN each project's containers, databases, and networks are fully isolated from each other and from prod.
AND resource consumption stays within the Beelink's capacity (no OOM kills, no CPU starvation).
AND spinning down one project's dev environment does not affect the other.
```

## Resolved Design Decisions

### 1. Behavioral auditor runtime
**Decision:** Claude Code CLI in a Docker container with restricted context. Invoked via `claude -p --model sonnet` (routine) or `--model opus` (deep validation). Max subscription auth mounted read-only -- no API costs. Token limit per run: configurable via `AUDITOR_MAX_TOKENS` (default 50k routine, 200k deep). Usage reported in output JSON.

### 2. Dev/prod model in Dokploy
**Decision:** Dokploy Environments within the same Project (not separate projects). Prod environment stays as-is. Dev environments created on demand via `project-duplicate` API, destroyed when done. Each service's env vars are independent per environment.

### 3. Obsidian monitoring folder
**Decision:** `monitoring/` folder at vault root (`/opt/vault/second-brain/monitoring/`). Filename: `{service-name}_{YYYY-MM-DD}_{short-slug}.md`. Frontmatter template with fields: source, service, severity (critical/warning/info), date, status (open/resolved). Sections: Observation, Expected Behavior, Evidence, Suggested Action.

### 4. Notification routing by severity
**Decision:** Fanout routing based on severity level:
- **critical**: Discord (red embed) + Email + Vault monitoring file
- **warning**: Discord (yellow embed) + Vault monitoring file
- **info**: Vault monitoring file only
- **success**: Discord (green embed) only, no email, no vault file

### 5. Dev database state
**Decision:** New projects start with empty DB; pipeline populates via init scripts in compose. Existing projects get schema from the same init scripts (not a prod data copy). Auditor validates against whatever state the build produces.

### 6. Catchup scope
**Decision:** Pull-based pipelines (scrapers, ETL): watermark pattern -- track last success, process from there. Push-based services: log gaps and alert, no replay possible. Reference implementation: ETL `--catchup` mode.

### 7. Monitor `--exec` for service execution
**Decision:** The `monitor` command supports an `--exec` flag that runs a command via `docker exec` on the service's container before auditing. This replaces per-service shell wrapper scripts (`run-and-audit.sh`) with a single CLI entry point. Container names are resolved from a service registry in config. If exec fails, the audit still runs. If the container isn't running, a `critical` notification fires and the command exits (no audit). The auditor has a 10-minute timeout to prevent runaway token burn. Crontab entries become a single `workflow-orchestrate monitor --exec ...` call.

### 8. AI cost management
**Decision:** Max subscription only (no API costs). Sonnet 4.6 for routine audits, Opus for deep work. Token limit per auditor run with usage reported in output JSON. No monthly budget ceiling needed since Max subscription is flat-rate.

### 9. Two-stage auditor architecture
**Decision:** The behavioral auditor is split into three execution phases to eliminate Claude's direct network access. See dedicated spec section below.

---

# Two-Stage Auditor Hardening Spec

## System Overview

The behavioral auditor runs Claude CLI inside Docker containers to verify that production services (bid-scraper, ETL) behave according to their specs. The current single-stage design gives Claude direct network access, which led to unauthorized network scanning (subnet scan of 10.0.1.0/24 on 2026-02-24). This redesign splits the auditor into a planning stage (Claude reasons, no network), a validated execution stage (dumb runner, restricted network), and an analysis stage (Claude evaluates, no network). The invariant: even if Claude ignores every instruction or gets prompt-injected, the worst outcome is a failed audit -- never unauthorized access.

## Behavioral Contract

### Invocation
- When `workflow-orchestrate monitor` triggers an audit, the two-stage auditor runs identically to the old single-stage auditor from the operator's perspective.
- When an audit completes, the report is written to the same archive path (`~/audit-reports/{service}/{mode}_{timestamp}/`) in the same format as before.
- When an audit completes, notifications are sent through the same channels (Discord via workflow-notify) as before.

### Stage 1: Planning
- When the planner container starts, it runs with `--network none` (no network access whatsoever).
- When Claude receives the behavioral spec and access document, it produces a query plan as structured JSON written to the output volume.
- When the query plan is produced, each entry specifies a query type (`psql` or `curl`), a target host, and the exact query/request to run.
- When the planner container runs Claude CLI, it does NOT use `--dangerously-skip-permissions`.

### Validation (Host-Side)
- When the validator receives a query plan, it checks every entry against the access document's declared hosts and URLs.
- When a query plan entry targets a host not in the access document, the entire audit fails immediately.
- When a query plan entry contains a SQL statement that does not match the strict allowlist pattern (`^SELECT \* FROM [a-zA-Z_][a-zA-Z0-9_.]*;?$`), the entire audit fails immediately. Only full-table SELECT statements are permitted. The dot in the character class supports schema-qualified table names (e.g., `gold.forecast_depletion`, `public.bid_opportunities`).
- When a query plan entry is a `curl` request, the full URL must exactly match one of the allowed URLs listed in the access document. No wildcards, no partial matching -- the URL in the plan must be character-for-character identical to an entry in the allowlist.
- When an access document specifies an empty URL allowlist (e.g., defendershield-etl), any `curl` entry in the query plan causes immediate failure.
- When a query plan entry contains malformed JSON or does not conform to the expected schema, the entire audit fails immediately.
- When validation fails for any reason, the operator is notified with the rejection reason and no queries are executed.

### Stage 2: Execution
- When the executor runs, the orchestrator creates a temporary Docker network, attaches only the executor container and the target service container(s) to it, executes the queries, and tears down the network afterward. The executor never joins `dokploy-network`.
- When the target container is already on `dokploy-network`, the orchestrator attaches it to the temporary network as an additional network (containers support multiple networks simultaneously). The target's existing connectivity is unaffected.
- When the executor runs approved queries, it connects only to the hosts specified in the validated plan.
- When a database is unavailable or a query fails, the entire audit fails immediately and the operator is notified.
- When all queries succeed, results are written as files to a shared volume for Stage 3 to read.
- When the executor runs, it has no AI, no Claude CLI, and no decision-making capability. It executes exactly the approved plan, nothing more.
- When the executor finishes (success or failure), the orchestrator detaches the target container from the temporary network and removes the temporary network. Cleanup happens even on failure.

### Stage 3: Analysis
- When the analysis container starts, it runs with `--network none` (no network access whatsoever).
- When Claude receives the query results and behavioral spec, it evaluates the results against the spec and produces the audit report.
- When the report is produced, it is written to the output volume in the same format as the current auditor produces.
- When the analysis container runs Claude CLI, it does NOT use `--dangerously-skip-permissions`.

### Timeouts
- When the total wall-clock time across all three stages exceeds 20 minutes, the audit fails immediately and the operator is notified.
- When any individual stage appears to hang, the parent process kills it and fails the audit.

### Query Strategy
- When the planner generates queries, it should request full table dumps (`SELECT * FROM {table}`) for every table listed in the access document, rather than crafting targeted queries. This eliminates the need for follow-up query rounds.

## Explicit Non-Behaviors

- The system must not allow Claude to execute any command against the network at any stage, because the entire point of this redesign is separating reasoning from execution.
- The system must not allow write operations (INSERT, UPDATE, DELETE, etc.) against any database, because the auditor's role is observation only.
- The system must not allow follow-up query rounds after Stage 2, because the initial full-table dumps provide all data needed for analysis.
- The system must not use `--dangerously-skip-permissions` in Claude CLI invocation, because this flag disables the safety mechanisms that prevent unauthorized actions.
- The system must not install nmap, netcat, or other network reconnaissance tools in any container, because these serve no legitimate auditing purpose and expand the attack surface.
- The system must not install general-purpose scripting languages in the executor container, because the executor must be a dumb runner with no flexibility beyond running the validated plan.
- The system must not produce partial audit reports from incomplete data, because partial results create false confidence. Fail or succeed, never half-succeed.
- The system must not fall back to the old single-stage auditor if the two-stage pipeline fails, because that reintroduces the exact vulnerabilities being eliminated.

## Integration Boundaries

### Claude CLI (Stages 1 and 3)
- **In:** Behavioral spec (markdown), access document (markdown), query results (JSON, Stage 3 only).
- **Out:** Query plan (JSON, Stage 1), audit report (markdown, Stage 3).
- **Invocation:** `claude --print` with `--model`, `--output-format text`, `--system-prompt`, `--allowedTools` (scoped to Read and file output only, no Bash), `--no-session-persistence`.
- **Failure mode:** If Claude CLI returns non-zero or produces unparseable output, the audit fails immediately.

### Target Databases (Stage 2 only)
- **In:** SQL SELECT queries from the validated plan.
- **Out:** Query result sets as JSON files.
- **Connection:** `psql` with connection string scoped to the specific host from the access document.
- **Auth:** Uses existing credentials (auditor_ro roles where configured, trust auth where applicable).
- **Failure mode:** If any connection or query fails, the audit fails immediately.

### Target HTTP Services (Stage 2 only)
- **In:** curl commands from the validated plan.
- **Out:** HTTP response bodies as JSON files.
- **Failure mode:** If any request fails or returns a non-2xx status, the audit fails immediately.

### Workflow-Notify (Host-Side)
- **In:** Audit outcome (pass/fail), report path, failure reason if applicable.
- **Out:** Discord notification.
- **Failure mode:** If notification fails, log the error but do not fail the audit (the report is already written).

### Report Archive (Host Filesystem)
- **In:** Completed report from Stage 3.
- **Out:** Files written to `~/audit-reports/{service}/{mode}_{timestamp}/`.
- **Same format and path convention as the current auditor.**

## Behavioral Scenarios

### Happy Path

```
; Full audit completes successfully with all scenarios passing.
GIVEN a production service with a behavioral spec containing 10 scenarios.
AND an access document listing one database host and three tables.
WHEN the auditor is triggered by the orchestrator.
THEN the operator receives a report evaluating all 10 scenarios.
AND the report is archived in the standard location.
AND a notification is sent with the audit summary.
```

```
; Audit detects a legitimate behavioral failure in the target service.
GIVEN a production service where one behavioral scenario is currently failing.
WHEN the auditor runs against that service.
THEN the report identifies the failing scenario with evidence from the query results.
AND the notification indicates the audit found failures.
```

```
; Audit invocation is identical to the old single-stage auditor.
GIVEN an operator who triggers an audit via workflow-orchestrate monitor.
WHEN the audit completes.
THEN the report location, format, and notification channel are indistinguishable from the previous auditor version.
```

### Error Scenarios

```
; Planner attempts to target an unauthorized host.
GIVEN an access document that lists only "bid-scraper-postgres" as an allowed host.
WHEN the planner produces a query plan targeting "ds-etl-postgres".
THEN the audit fails before any queries are executed.
AND the operator is notified that validation rejected an unauthorized host.
AND no network connections are made to any database.
```

```
; Target database is unreachable during execution.
GIVEN a valid query plan targeting "bid-scraper-postgres".
WHEN the executor cannot connect to that database.
THEN the audit fails immediately.
AND the operator is notified of the connection failure.
AND no partial report is produced.
```

### Edge Cases

```
; Planner produces a query that deviates from the strict SELECT * FROM pattern.
GIVEN a query plan containing "SELECT * FROM dblink('host=other-db', 'DELETE FROM users')".
WHEN the validator checks the query against the allowlist regex.
THEN the audit fails before any queries are executed.
AND the operator is notified that the query did not match the allowed pattern.
```

```
; Planner produces a query with a WHERE clause (not permitted even though read-only).
GIVEN a query plan containing "SELECT * FROM bid_opportunities WHERE status = 'open'".
WHEN the validator checks the query against the allowlist regex.
THEN the audit fails before any queries are executed.
AND the operator is notified that the query did not match the allowed pattern.
```

```
; Planner produces a curl request to a URL not in the access document.
GIVEN an access document for bid-scraper that lists "https://hillsboroughcounty.bonfirehub.com/api/..." as an allowed URL.
WHEN the planner produces a query plan with a curl entry targeting "https://evil.com/exfiltrate".
THEN the audit fails before any HTTP requests are made.
AND the operator is notified that the URL did not match the allowlist.
```

```
; Planner produces a curl request for a service with an empty URL allowlist.
GIVEN an access document for defendershield-etl that lists zero allowed URLs.
WHEN the planner produces a query plan containing any curl entry.
THEN the audit fails before any HTTP requests are made.
AND the operator is notified that curl requests are not permitted for this service.
```

```
; Total audit time exceeds the 20-minute budget.
GIVEN an audit where Stage 1 planning takes 12 minutes.
AND Stage 2 execution takes 9 minutes.
WHEN the cumulative wall-clock time crosses 20 minutes during Stage 2.
THEN the executor is killed immediately.
AND the audit fails with a timeout notification to the operator.
AND no partial report is produced.
```

## Resolved Ambiguities

1. **Query plan JSON schema.** Implementation proposes a schema during development; reviewed before merge. The planner's system prompt will include the schema definition so Claude produces conformant output.

2. **Executor is a Docker container** started via `docker run --rm` by the host-side orchestrator (not managed by Dokploy, since it's ephemeral). Runs on a purpose-built Docker network scoped to only the target database host -- not on `dokploy-network`. Contains only `psql` and `curl` (minimal alpine image), no AI, no scripting languages.

3. **Claude CLI output via stdout.** Stages 1 and 3 use `claude --print` which writes to stdout. The entrypoint script captures stdout and writes the query plan (Stage 1) or report (Stage 3) to the output volume. Claude gets `--allowedTools "Read"` only -- no Bash, no Write. This is the simplest approach and eliminates file-write permissions entirely.

4. **Strict SQL allowlist (not denylist).** The validator only accepts queries matching `^SELECT \* FROM [a-zA-Z_][a-zA-Z0-9_.]*;?$`. The dot in the character class supports schema-qualified names (e.g., `gold.forecast_depletion`, `public.bid_opportunities`) which are in active use in the ETL database. Since the query strategy is full-table dumps, there is no legitimate reason for any other query shape. This is ungameable -- no amount of creative SQL can pass this regex and cause harm.

7. **Curl URL validation is exact-match against access document.** Each access document lists the full URLs the auditor may request (e.g., Bonfire API endpoints for bid-scraper). The validator requires character-for-character match -- no wildcards, no partial matching, no query parameter flexibility. Services with no HTTP endpoints (like defendershield-etl) specify an empty URL allowlist, which causes any curl entry to fail validation.

5. **Credentials via environment variables at `docker run` time.** The host-side orchestrator reads database credentials from a config file on the host (populated from Dokploy env vars). Credentials are passed to the executor container as `-e` flags. Claude never sees credentials at any stage -- the planner specifies host and table names only, the orchestrator resolves credentials, and the executor receives them as env vars.

6. **Delete the old single-stage auditor code** once the two-stage auditor is validated and passing all acceptance tests. Verify no other code depends on it before removal.

## Implementation Constraints

- Must live in the `workflow-platform` repository alongside the existing auditor code.
- Python, type hints, Google-style docstrings, PEP 8.
- Planner/analysis containers may use python3 for the entrypoint (captures Claude stdout, writes to output volume). The security boundary is `--network none` + `--allowedTools "Read"`, not the absence of scripting languages.
- Executor container uses a minimal alpine image with only `psql` and `curl`. No python3, no scripting languages, no AI.
- All containers run with `--cap-drop ALL` and `--network none` (except executor, which gets scoped network access).
- Must pass ruff, pyright, and pytest.
- Conventional commits, branch naming per existing workflow.
