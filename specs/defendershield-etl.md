# DefenderShield ETL Behavioral Specification

## Scenario 1: Database connectivity
GIVEN the ETL service has a running database.
WHEN a connection is attempted using the provided credentials.
THEN the database accepts connections and responds to queries.

## Scenario 2: Silver layer has sales data
GIVEN the ETL ingests sales data from multiple marketplaces.
WHEN the silver.fact_sales_items table is queried.
THEN there are at least 100,000 line items spanning multiple years.

## Scenario 3: Sales data is fresh
GIVEN the ETL runs daily in catchup mode.
WHEN the most recent sale_date in silver.fact_sales_items is checked.
THEN data exists for at least one date within the past 2 days.

## Scenario 4: Multiple marketplaces represented
GIVEN DefenderShield sells across multiple channels.
WHEN distinct marketplaces in silver.fact_sales_items are counted.
THEN there are at least 3 distinct marketplaces.

## Scenario 5: Gold snapshot is populated
GIVEN the ETL produces a completed sales snapshot.
WHEN gold.completed_sales_items_snapshot is queried.
THEN it contains at least 100,000 records.

## Scenario 6: Forecast depletion is current
GIVEN inventory forecasts are regenerated on each ETL run.
WHEN gold.forecast_depletion is queried.
THEN the forecast_date is within the past 2 days for at least one SKU.

## Scenario 7: Forecast classifications are valid
GIVEN SKUs are classified by depletion risk.
WHEN the classification column in gold.forecast_depletion is examined.
THEN every row has a non-null classification value.

## Scenario 8: Monthly aggregations exist
GIVEN the ETL produces monthly sales rollups.
WHEN silver.monthly_sales_by_sku is queried.
THEN there are aggregation rows for at least 12 distinct months.

## Scenario 9: Price data integrity
GIVEN sales items have price fields.
WHEN silver.fact_sales_items is checked for price anomalies.
THEN less than 1% of records have a null unit_price where quantity is greater than 0.

## Scenario 10: No duplicate line items
GIVEN the ETL uses upsert logic to prevent duplicates.
WHEN potential duplicates are checked (same order_id, sku, item_source, _modified_date).
THEN zero duplicates exist (the unique constraint holds).
