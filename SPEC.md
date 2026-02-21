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

### 7. AI cost management
**Decision:** Max subscription only (no API costs). Sonnet 4.6 for routine audits, Opus for deep work. Token limit per auditor run with usage reported in output JSON. No monthly budget ceiling needed since Max subscription is flat-rate.

## Implementation Constraints

- **Runtime:** Beelink Ser3 Mini (limited CPU/RAM — all services share resources)
- **Container orchestration:** Dokploy v0.26.6 (existing, stays as-is)
- **Reverse proxy:** Traefik via Dokploy, SSL via Cloudflare
- **AI interface:** Claude Code CLI over SSH
- **Language:** Python for all custom tooling (type hints, Google-style docstrings, PEP 8)
- **Code style:** Simple functions over classes, modular design, environment variables for all config
- **Git workflow:** Conventional commits, feature branches, GitHub CLI
- **Networking:** All containers on `dokploy-network`, unique hostnames per Postgres instance
- **Secrets:** Dokploy environment variables only, never in git
- **Monitoring storage:** Obsidian vault at `/opt/vault/second-brain/`
- **Notifications:** Discord webhooks + Gmail SMTP (existing infrastructure)
