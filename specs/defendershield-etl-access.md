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
| shipping_country | text | Destination country |
| _modified_date | date | Source modification date |
| _created_at | timestamptz | ETL ingestion time |
| raw_unitprice_a | numeric(10,2) | Original unit price before FX |
| raw_lineprice | numeric(10,2) | Original line price before FX |

Unique constraint: (order_id, sku, item_source, _modified_date)

### silver.monthly_sales_by_sku
Pre-aggregated monthly sales rollup per SKU.

### gold.completed_sales_items_snapshot
Completed/shipped orders snapshot for reporting.

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

### gold.monthly_inventory_sales
Monthly inventory and sales summary.

### gold.product_info_bi
Product master data for BI dashboards.

### gold.vw_powerbi_sales
View for Power BI reporting.

### gold.completed_sales_items_snapshot_bi
BI-optimized snapshot.

## Key Data Facts

- Sales data range: 2017-12-30 to present
- ~208k line items in silver.fact_sales_items
- ~202k completed items in gold snapshot
- 142 SKUs tracked in forecast_depletion
- 8 distinct marketplaces
- ~5,460 monthly SKU aggregation rows

## Service Schedule

- ETL runs daily at 6:15 AM ET via `workflow-orchestrate monitor --exec` in `--catchup` mode
- Catchup processes all unprocessed silver orders (self-healing)
- Container: ds-etl-nhdcjb-etl-scheduler-1
- Docker hostname on dokploy-network: ds-etl-postgres (DB)
