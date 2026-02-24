# DefenderShield ETL Behavioral Specification

Note: The auditor evaluates these scenarios by reading raw psql text output
(SELECT * truncated to ~4KB per table, with total row count appended) and
the access document schema. The analyzer has NO ability to run code, sort,
filter, or aggregate data -- it can only inspect the literal text output.
Scenarios must be verifiable from what is visible in the first few rows of
each table plus the total row count.

## Scenario 1: Database connectivity
GIVEN the ETL service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: Silver layer record volume
GIVEN the ETL ingests sales data from multiple marketplaces.
WHEN the silver.fact_sales_items table row count is checked.
THEN there are at least 100,000 total line items.

## Scenario 3: Sales data spans multiple years
GIVEN DefenderShield has been selling products since 2017.
WHEN the sampled silver.fact_sales_items rows are examined.
THEN at least 2 distinct sale_date years are visible in the sample.

## Scenario 4: Multiple marketplaces represented
GIVEN DefenderShield sells across multiple channels.
WHEN the marketplace column in sampled silver.fact_sales_items rows is examined.
THEN at least 2 distinct marketplace values are visible in the sample.

## Scenario 5: Gold snapshot is populated
GIVEN the ETL produces a completed sales snapshot.
WHEN the gold.completed_sales_items_snapshot table row count is checked.
THEN it contains at least 100,000 records.

## Scenario 6: Forecast depletion is current
GIVEN inventory forecasts are regenerated on each ETL run.
WHEN the sampled gold.forecast_depletion rows are examined.
THEN at least some rows have a forecast_date within the past 7 days.

## Scenario 7: Forecast classifications are valid
GIVEN SKUs are classified by depletion risk.
WHEN the sampled gold.forecast_depletion rows are examined.
THEN all visible rows have a non-null classification value.

## Scenario 8: Monthly aggregations exist
GIVEN the ETL produces monthly sales rollups.
WHEN the silver.monthly_sales_by_sku table row count is checked.
THEN there are at least 1,000 aggregation rows.

## Scenario 9: Sales data integrity -- required fields present
GIVEN sales items have key identifying fields.
WHEN the sampled silver.fact_sales_items rows are checked.
THEN all visible rows have non-null order_id, sku, and quantity fields.

## Scenario 10: Gold snapshot freshness
GIVEN the gold snapshot is rebuilt on each ETL run.
WHEN the sampled gold.completed_sales_items_snapshot rows are examined.
THEN the _snapshot_date is within the past 7 days for at least some rows.
