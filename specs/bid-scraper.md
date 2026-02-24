# Bid Scraper Behavioral Specification

Note: The auditor evaluates these scenarios by reading raw psql text output
(SELECT * truncated to ~4KB per table, with total row count appended) and
full API curl responses. The analyzer has NO ability to run code, sort, filter,
or aggregate data -- it can only inspect the literal text output. Scenarios
must be verifiable from what is visible in the first few rows of each table
plus the total row count.

## Scenario 1: Database connectivity
GIVEN the bid scraper service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: API endpoint reachability
GIVEN the bid scraper pulls data from Bonfire portal APIs.
WHEN the allowed API endpoints for Hillsborough and Pasco counties are called.
THEN all endpoints return valid JSON responses with a success indicator.

## Scenario 3: Procurement record volume
GIVEN the bid scraper has been running daily for multiple weeks.
WHEN the bid_opportunities table row count is checked.
THEN there are at least 500 total procurement records.

## Scenario 4: Contract record volume
GIVEN the bid scraper captures awarded contracts from both counties.
WHEN the public_contracts table row count is checked.
THEN there are at least 500 total contract records.

## Scenario 5: Scrape freshness
GIVEN the bid scraper runs on a daily schedule.
WHEN the most recent scrape_runs entry is examined.
THEN the most recent run with status "success" has a finished_at timestamp within the past 72 hours.

## Scenario 6: Recent scrape reliability
GIVEN the bid scraper should succeed on most runs.
WHEN the visible scrape_runs entries are examined.
THEN the most recent completed runs have status "success" (no string of consecutive failures).

## Scenario 7: Data integrity -- opportunities have required fields
GIVEN procurement records are ingested from external portals.
WHEN the sampled bid_opportunities rows are checked.
THEN all visible rows have non-null project_id, project_name, and source fields.

## Scenario 8: Data integrity -- contracts have valid dates
GIVEN contracts have start and end date fields.
WHEN the sampled public_contracts rows with non-null dates are examined.
THEN no visible contract has a start_date after its end_date.

## Scenario 9: Data freshness -- records updated recently
GIVEN the scraper updates records on each run.
WHEN the last_updated timestamps in sampled bid_opportunities rows are checked.
THEN at least some records have been updated within the past 72 hours.

## Scenario 10: Vendor data present
GIVEN the bid scraper captures vendor records alongside contracts.
WHEN the vendors table row count is checked.
THEN there are at least 100 vendor records with non-null vendor_name fields.
