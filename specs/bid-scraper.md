# Bid Scraper Behavioral Specification

## Scenario 1: Database connectivity
GIVEN the bid scraper service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: Opportunity volume
GIVEN the bid scraper ingests open and past opportunities from Hillsborough County.
WHEN the bid_opportunities table total row count is computed.
THEN there are at least 500 total opportunity records.

## Scenario 3: Contract volume
GIVEN the bid scraper captures awarded contracts from Hillsborough County.
WHEN the public_contracts table total row count is computed.
THEN there are at least 500 total contract records.

## Scenario 4: Scrape freshness
GIVEN the bid scraper runs on a daily schedule.
WHEN the most recent scrape_runs entry is examined.
THEN the most recent run with status "success" has a finished_at timestamp within the past 72 hours.

## Scenario 5: Scrape success rate
GIVEN the bid scraper should succeed on most runs.
WHEN the scrape_runs entries over the last 14 days are analyzed.
THEN at least 80% of completed runs have status "success".

## Scenario 6: Required fields integrity
GIVEN procurement records are ingested from external portals.
WHEN the bid_opportunities table is analyzed for null required fields.
THEN at least 95% of rows have non-null project_id, project_name, and source fields.

## Scenario 7: Contract date validity
GIVEN contracts have start and end date fields.
WHEN the public_contracts table is analyzed for date consistency.
THEN zero contracts have a start_date after their end_date.

## Scenario 8: Data freshness -- records updated recently
GIVEN the scraper updates records on each run.
WHEN the last_updated timestamps in bid_opportunities are analyzed.
THEN records have been updated within the past 72 hours.

## Scenario 9: Vendor completeness
GIVEN the bid scraper captures vendor records alongside contracts.
WHEN the vendors table is analyzed.
THEN there are at least 500 vendor records, all with non-null vendor_name fields.
