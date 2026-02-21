# Phase 2: Catchup Pattern Standardization -- Acceptance Specs

## Spec 1: Gap detection fires warning when stale

; The bid scraper raises a warning when its last successful run is too old.
GIVEN the bid scraper has a history of successful runs.
WHEN the most recent successful run finished more than 36 hours ago.
THEN a warning notification fires via workflow-notify with service="bid-scraper".
AND the notification includes when the last successful run occurred.

## Spec 2: No warning when recent run exists

; No false alarm when the scraper is running on schedule.
GIVEN the bid scraper's most recent successful run finished 12 hours ago.
WHEN the gap detection check runs.
THEN no notification is sent.

## Spec 3: Warning when no successful runs exist at all

; First-time or wiped database triggers a warning.
GIVEN the scrape_runs table has no rows with status "success".
WHEN the gap detection check runs.
THEN a warning notification fires indicating no successful runs were found.

## Spec 4: Gap detection is non-fatal on DB errors

; If the database is unreachable, the check logs and notifies but does not crash.
GIVEN the bid scraper database is unreachable.
WHEN the gap detection check runs.
THEN a critical notification fires indicating the database is unavailable.
AND the check completes without raising an unhandled exception.
