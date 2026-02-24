# DefenderShield ETL Service Access

## Database

- Host: ds-etl-postgres
- Port: 5432
- Database: defendershield
- User: auditor_ro
- Password: (none -- trust auth on internal Docker network)

Connection command:
```
psql -h ds-etl-postgres -p 5432 -U auditor_ro -d defendershield
```

## Schemas

The ETL uses a medallion architecture: silver (cleaned/deduped) and gold (aggregated/analytics-ready).

### silver.fact_sales_items
Individual line items from all sales channels.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| order_id | text | Order identifier |
| sku | text | Product SKU |
| quantity | integer | Units sold |
| unit_price | numeric(10,2) | Price per unit (USD) |
| line_price | numeric(10,2) | Total line amount (USD) |
| marketplace | text | Sales channel (e.g. "Amazon", "Shopify") |
| status | text | Order status |
| sale_date | date | Date of sale |
| sale_timestamp | timestamptz | Exact sale time |
| item_source | text | Data source identifier |
| part_number | text | Product part number |
| shipping_country | text | Destination country |
| shipping_region | text | Destination region/state |
| shipping_city | text | Destination city |
| _modified_date | date | Source modification date |
| _created_at | timestamptz | ETL ingestion time |
| raw_unitprice_a | numeric(10,2) | Original unit price before FX |
| raw_lineprice | numeric(10,2) | Original line price before FX |

Unique constraint: (order_id, sku, item_source, _modified_date)

### silver.monthly_sales_by_sku
Pre-aggregated monthly sales rollup per SKU.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| sku | text | Product SKU |
| classification | text | Product classification |
| month_of_sale | text | Month name |
| year_of_sale | integer | Year |
| total_quantity | integer | Units sold in month |
| total_revenue | numeric(12,2) | Revenue in month |
| order_count | integer | Number of orders |
| _updated_at | timestamptz | Last update time |

Unique constraint: (sku, month_of_sale, year_of_sale)

### gold.completed_sales_items_snapshot
Completed/shipped orders snapshot for reporting.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| order_id | text | Order identifier |
| sku | text | Product SKU |
| quantity | integer | Units sold |
| unit_price | numeric(10,2) | Price per unit (USD) |
| line_price | numeric(10,2) | Total line amount (USD) |
| marketplace | text | Sales channel |
| sale_date | date | Date of sale |
| sale_timestamp | timestamptz | Exact sale time |
| category | text | Product category |
| short_description | text | Product description |
| shipping_country | text | Destination country |
| shipping_region | text | Destination region/state |
| shipping_city | text | Destination city |
| item_source | text | Data source identifier |
| _snapshot_date | date | Date snapshot was built |
| part_number | text | Product part number |
| raw_unitprice_a | numeric(10,2) | Original unit price before FX |
| raw_lineprice | numeric(10,2) | Original line price before FX |

Unique constraint: (order_id, sku, item_source)

### gold.forecast_depletion
Inventory depletion forecasts per SKU.

| Column | Type | Notes |
|--------|------|-------|
| sku | text PK | Product SKU |
| classification | text | e.g. "healthy", "at_risk", "critical" |
| quantity_on_hand | integer | Current inventory |
| quantity_incoming | integer | Incoming inventory |
| months_to_depletion | numeric(5,2) | Months until stockout |
| months_to_depletion_with_incoming | numeric(5,2) | With incoming stock |
| method_used | text | Forecasting method |
| forecast_date | date | Date of forecast |
| _updated_at | timestamptz | Last update time |
| seas_mtd | numeric(5,2) | Seasonal months-to-depletion |
| imp_month_cnt | integer | Months with imputed data |
| total_12m_sales | numeric(10,2) | Trailing 12-month sales |
| insuff_data | boolean | Insufficient data flag |
| simple_mtd | numeric(5,2) | Simple months-to-depletion |
| depl_diff | numeric(5,2) | Depletion method difference |

### gold.monthly_inventory_sales
Monthly inventory and sales summary.

| Column | Type | Notes |
|--------|------|-------|
| sku | text PK (composite) | Product SKU |
| month_name | text PK (composite) | Month name |
| website_sales | integer | Website channel sales |
| amazon_sales | integer | Amazon channel sales |
| year_sales | integer | Annual sales total |
| _updated_at | timestamptz | Last update time |

### gold.product_info_bi
View: Product master data for BI dashboards.

### gold.vw_powerbi_sales
View: Power BI reporting view.

### gold.completed_sales_items_snapshot_bi
View: BI-optimized snapshot.

## Key Data Facts

- Sales data range: 2017-12-30 to present
- ~208k line items in silver.fact_sales_items
- ~202k completed items in gold snapshot
- 142 SKUs tracked in forecast_depletion
- 8 distinct marketplaces
- ~5,460 monthly SKU aggregation rows
- ~432 monthly inventory sales rows

## Allowed URLs

No HTTP endpoints are used by this service. The URL allowlist is empty. Any curl entry in a query plan for this service must be rejected by the validator.

## Service Schedule

- ETL runs daily at 6:15 AM ET via `workflow-orchestrate monitor --exec` in `--catchup` mode
- Catchup processes all unprocessed silver orders (self-healing)
- Container: ds-etl-nhdcjb-etl-scheduler-1
- Docker hostname on dokploy-network: ds-etl-postgres (DB)
