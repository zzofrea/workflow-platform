# DefenderShield ETL Behavioral Specification

Note: The analyzer has Bash access and CAN run Python, jq, and grep on the
full executor results (executor_results.json). This enables row counting,
percentage-based integrity checks, and full-table analysis. Scenarios should
use concrete numeric thresholds that the analyzer can verify programmatically.

## Scenario 1: Database connectivity
GIVEN the ETL service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries for all documented tables.

## Scenario 2: Silver layer record volume
GIVEN the ETL ingests sales data from multiple marketplaces.
WHEN the silver.fact_sales_items table row count is computed.
THEN there are at least 100,000 total line items.

## Scenario 3: Sales data freshness
GIVEN the ETL runs daily and ingests recent orders.
WHEN the silver.fact_sales_items table is analyzed for recency.
THEN there are rows with _created_at within the past 7 days.

## Scenario 4: Marketplace diversity
GIVEN DefenderShield sells across multiple channels.
WHEN the distinct marketplace values in silver.fact_sales_items are counted.
THEN there are at least 3 distinct marketplaces.

## Scenario 5: Gold snapshot volume
GIVEN the ETL produces a completed sales snapshot.
WHEN the gold.completed_sales_items_snapshot table row count is computed.
THEN it contains at least 100,000 records.

## Scenario 6: Forecast freshness
GIVEN inventory forecasts are regenerated on each ETL run.
WHEN the gold.forecast_depletion table is analyzed for recency.
THEN at least some rows have a forecast_date within the past 7 days.

## Scenario 7: Forecast classification completeness
GIVEN SKUs are classified by depletion risk.
WHEN the gold.forecast_depletion table is analyzed for null classifications.
THEN all rows have a non-null classification value.

## Scenario 8: Monthly aggregation coverage
GIVEN the ETL produces monthly sales rollups.
WHEN the distinct months in silver.monthly_sales_by_sku are counted.
THEN there are at least 12 distinct months.

## Scenario 9: Required fields integrity
GIVEN sales items have key identifying fields.
WHEN the silver.fact_sales_items table is analyzed for null required fields.
THEN at least 95% of rows have non-null order_id, sku, and quantity fields.

## Scenario 10: Price data integrity
GIVEN sales items should have pricing data when quantities are present.
WHEN rows with quantity > 0 are analyzed for null unit_price.
THEN less than 1% of such rows have a null unit_price.
