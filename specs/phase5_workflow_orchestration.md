# Phase 5: Workflow Orchestration

## Spec 1: Build command runs the full dev cycle
; The build command chains: verify spec exists, spin up dev, signal the builder, run auditor.
GIVEN a service has a behavioral spec and access document.
WHEN the orchestrator runs the build command for that service.
THEN a dev environment is created (or reused if one exists).
AND the auditor runs against the dev environment.
AND a report is presented to the human.

## Spec 2: Deploy command requires passing audit
; No deployment without a green auditor report.
GIVEN the auditor has run and produced a report.
WHEN the orchestrator runs the deploy command.
THEN deployment proceeds only if the latest audit report shows "pass".
AND if the report shows "fail" or "error", deployment is blocked with a clear message.

## Spec 3: Deploy command requires human confirmation
; Humans must explicitly approve every production deployment.
GIVEN the audit report shows "pass".
WHEN the orchestrator reaches the deployment step.
THEN the script pauses and asks for explicit human confirmation.
AND deployment proceeds only after the human types approval.
AND if the human declines, no deployment happens and no state changes.

## Spec 4: Deploy triggers git push and notifies
; Deployment is a git push to main, followed by a notification.
GIVEN the human has confirmed deployment.
WHEN the deploy step executes.
THEN the code is pushed to the main branch (which triggers Dokploy auto-deploy).
AND a success notification is sent confirming the deployment.
AND the dev environment is offered for teardown.

## Spec 5: Monitor command runs auditor in prod mode
; Scheduled monitoring uses the same auditor but against prod.
GIVEN a service has a behavioral spec and prod access document.
WHEN the orchestrator runs the monitor command.
THEN the auditor runs in prod mode against the live service.
AND findings are routed through the notification hub.
AND the report is stored as an artifact.

## Spec 6: Each step produces artifacts
; The workflow creates a paper trail at every stage.
GIVEN the full workflow runs (build, audit, deploy).
WHEN each step completes.
THEN the spec file, audit report, and deployment notification are all persisted.
AND a human can reconstruct what happened from the artifacts alone.
