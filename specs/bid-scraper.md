# Bid Scraper Behavioral Specification

## Scenario 1: Database connectivity
GIVEN the bid scraper service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries.

## Scenario 2: Source coverage
GIVEN the bid scraper is configured to scrape multiple county portals.
WHEN the sources table is queried.
THEN there are at least 2 enabled sources (Hillsborough and Pasco counties).

## Scenario 3: Procurement records exist per source
GIVEN the bid scraper has been running daily.
WHEN the opportunities table is queried per source.
THEN each enabled source has at least 100 procurement records.

## Scenario 4: Contract records exist per source
GIVEN the bid scraper captures awarded contracts.
WHEN the contracts table is queried per source.
THEN each enabled source has at least 50 contract records.

## Scenario 5: Scrape freshness
GIVEN the bid scraper runs on a daily schedule.
WHEN the most recent successful scrape run is checked.
THEN the last successful run completed within the past 72 hours.

## Scenario 6: Scrape success rate
GIVEN the bid scraper has been running daily.
WHEN scrape runs from the last 14 days are examined.
THEN at least 80% of completed runs have status "success".

## Scenario 7: Data integrity -- opportunities have required fields
GIVEN procurement records are ingested from external portals.
WHEN opportunities are checked for required fields.
THEN at least 95% of records have a non-null project_name and a non-null source_id.

## Scenario 8: Data integrity -- contracts have valid dates
GIVEN contracts have start and end date fields.
WHEN contracts with non-null date ranges are examined.
THEN no contract has a start_date after its end_date.

## Scenario 9: No stale-only data
GIVEN the scraper updates records on each run.
WHEN the most recent last_seen_at timestamp across opportunities is checked.
THEN at least some records have been seen (last_seen_at) within the past 72 hours.

## Scenario 10: Scrape errors are captured
GIVEN scrape runs may encounter errors.
WHEN failed scrape runs are examined.
THEN each failed run has a non-empty errors field explaining the failure.
