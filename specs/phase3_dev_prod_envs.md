# Phase 3: Dev/Prod Environment Model -- Acceptance Specs

## Spec 1: Spin up a dev environment from prod

; A dev environment can be created from a running prod service.
GIVEN a service is running in the production environment.
WHEN the user requests a dev environment for that service.
THEN a new environment appears in Dokploy with isolated containers for that service.
AND the dev environment's database uses a unique hostname distinct from prod.
AND the production service continues running unaffected.

## Spec 2: Spin down a dev environment

; Dev containers can be stopped to conserve resources.
GIVEN a dev environment is running for a service.
WHEN the user requests the dev environment be spun down.
THEN the dev containers stop.
AND the production service continues running unaffected.

## Spec 3: Destroy a dev environment

; Dev environments can be fully removed.
GIVEN a dev environment exists for a service (running or stopped).
WHEN the user requests the dev environment be destroyed.
THEN the dev environment is removed from Dokploy.
AND the production service continues running unaffected.

## Spec 4: List active dev environments

; The user can see which dev environments exist and their status.
GIVEN one or more dev environments exist.
WHEN the user lists dev environments.
THEN each dev environment is shown with its service name and status.

## Spec 5: Resource guard prevents overload

; The system warns before creating dev environments that would overload the host.
GIVEN the host is already running many containers or is low on RAM.
WHEN the user requests a new dev environment.
THEN a warning is displayed about resource constraints.
AND the user is asked to confirm before proceeding.

## Spec 6: Dev environment uses dev-specific configuration

; Dev environments have isolated configuration that does not collide with prod.
GIVEN a dev environment is created for a service with a database.
WHEN the dev containers start.
THEN the dev database hostname follows the pattern "{service}-dev-postgres".
AND the dev environment does not share database volumes with prod.
