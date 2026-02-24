# Two-Stage Auditor Hardening

## Spec 1: Planner produces a valid query plan without network access

; The planner reasons about what data to collect but cannot reach any service.
GIVEN a behavioral spec and access document are available as input files.
AND the planner container has no network connectivity.
WHEN the planner runs.
THEN a query plan JSON file appears in the output directory.
AND the plan contains one entry per table listed in the access document.
AND each entry specifies a query type, target host, and SQL query.
AND no network connections were attempted during the run.

## Spec 2: Validator accepts a conforming query plan

; Plans that follow the rules pass through unchanged.
GIVEN a query plan where every SQL entry matches `SELECT * FROM {table}`.
AND every host in the plan is listed in the access document.
AND every URL in the plan exactly matches an allowed URL in the access document.
WHEN the validator checks the plan.
THEN validation passes.
AND the plan is forwarded to the executor unchanged.

## Spec 3: Validator rejects a plan with an unauthorized host

; The host allowlist is the first line of defense.
GIVEN a query plan containing an entry targeting a host not in the access document.
WHEN the validator checks the plan.
THEN the entire audit fails before any queries execute.
AND the failure reason identifies the unauthorized host.
AND the operator receives a notification with the rejection details.

## Spec 4: Validator rejects a plan with a non-allowlisted SQL shape

; Only `SELECT * FROM table` is permitted. Everything else is rejected.
GIVEN a query plan containing a SQL statement that does not match the strict allowlist pattern.
WHEN the validator checks the plan.
THEN the entire audit fails before any queries execute.
AND the failure reason identifies the offending query.

## Spec 5: Validator rejects a plan with an unauthorized URL

; Curl requests must exactly match the access document URL allowlist.
GIVEN a query plan containing a curl entry whose URL is not in the access document.
WHEN the validator checks the plan.
THEN the entire audit fails before any queries execute.
AND the failure reason identifies the unauthorized URL.

## Spec 6: Validator rejects any curl entry when the URL allowlist is empty

; Services with no HTTP endpoints cannot have curl queries.
GIVEN an access document that lists zero allowed URLs.
AND a query plan containing at least one curl entry.
WHEN the validator checks the plan.
THEN the entire audit fails before any queries execute.
AND the failure reason indicates curl requests are not permitted for this service.

## Spec 7: Executor runs validated queries on an isolated network

; The executor connects only to the target, never to the broader network.
GIVEN a validated query plan targeting a specific database host.
WHEN the executor runs.
THEN a temporary Docker network is created.
AND the executor container and the target container are the only members of that network.
AND all queries execute against the target database.
AND the temporary network is removed after execution completes.

## Spec 8: Executor fails fast and cleans up on database unavailability

; No partial results, no orphaned networks.
GIVEN a validated query plan targeting a database that is currently stopped.
WHEN the executor attempts to connect.
THEN the audit fails immediately.
AND the temporary Docker network is removed despite the failure.
AND the operator receives a notification about the connection failure.
AND no result files are written.

## Spec 9: Analyzer produces a report without network access

; The analyzer evaluates data it receives, never fetches its own.
GIVEN query result files from the executor and the behavioral spec.
AND the analyzer container has no network connectivity.
WHEN the analyzer runs.
THEN a report file appears in the output directory with a result for each scenario.
AND the report is in the same format as the previous single-stage auditor.
AND no network connections were attempted during the run.

## Spec 10: Full pipeline produces the same output as the old auditor

; Backward compatibility -- the operator sees no difference.
GIVEN a production service with an existing behavioral spec and access document.
WHEN `workflow-orchestrate monitor` triggers an audit.
THEN a report is archived at `~/audit-reports/{service}/{mode}_{timestamp}/`.
AND a Discord notification is sent with the audit summary.
AND the report format is indistinguishable from the previous auditor version.

## Spec 11: Pipeline fails within the 20-minute timeout

; Runaway audits are killed, not tolerated.
GIVEN an audit where any stage takes longer than its share of the 20-minute budget.
WHEN the cumulative wall-clock time exceeds 20 minutes.
THEN the running stage is killed.
AND the audit fails with a timeout reason.
AND the operator is notified.
AND any temporary Docker networks are cleaned up.

## Spec 12: Credentials never appear in planner or analyzer context

; Claude never sees database passwords or API keys.
GIVEN the planner and analyzer containers run with only spec and access documents as input.
WHEN the planner generates a query plan.
THEN the plan contains host names and table names only, no credentials.
AND credentials are resolved by the host-side orchestrator and passed to the executor as environment variables.
AND no credential values appear in any file Claude can read.

## Spec 13: Old single-stage auditor code is removed

; No fallback to the insecure architecture.
GIVEN the two-stage auditor passes all acceptance tests.
WHEN the old single-stage auditor code is removed.
THEN no other module in the codebase imports or references the removed code.
AND the orchestrator invokes only the two-stage pipeline.
AND no `--dangerously-skip-permissions` flag appears anywhere in the codebase.
