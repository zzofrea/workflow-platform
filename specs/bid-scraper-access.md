# Bid Scraper Service Access

## Database

- Host: bid-scraper-postgres
- Port: 5432
- Database: bidscraper
- User: auditor_ro
- Password: auditor_ro_readonly

Connection command:
```
PGPASSWORD=auditor_ro_readonly psql -h bid-scraper-postgres -p 5432 -U auditor_ro -d bidscraper
```

## Tables

### sources
Configured scrape targets.

| Column | Type | Notes |
|--------|------|-------|
| source_id | text PK | e.g. "hillsborough", "pasco" |
| org_id | text | Bonfire org identifier |
| portal_host | text | e.g. "hillsboroughcounty.bonfirehub.com" |
| enabled | boolean | Whether source is actively scraped |
| last_scraped_at | timestamptz | Last successful scrape time |
| created_at | timestamptz | Row creation time |

### opportunities
Procurement listings scraped from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| source_id | text FK→sources | Which portal |
| project_id | text | Bonfire project ID |
| project_name | text | Listing title |
| date_close | timestamptz | Bid deadline |
| status_id | smallint | Bonfire status code |
| is_public_award | boolean | Public award flag |
| description | text | Full description |
| solicitation_type | text | e.g. "RFP", "ITB" |
| first_seen_at | timestamptz | When scraper first found it |
| last_seen_at | timestamptz | Most recent scrape that saw it |
| detail_scraped_at | timestamptz | When detail page was scraped |
| created_at | timestamptz | Row creation |
| updated_at | timestamptz | Last update |

Unique constraint: (source_id, project_id)

### contracts
Awarded contracts from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| source_id | text FK→sources | Which portal |
| contract_id | text | Bonfire contract ID |
| vendor_id | text | Vendor reference |
| name | text | Contract title |
| status_id | smallint | Contract status code |
| start_date | timestamptz | Contract start |
| end_date | timestamptz | Contract end |
| first_seen_at | timestamptz | When scraper first found it |
| last_seen_at | timestamptz | Most recent scrape |
| created_at | timestamptz | Row creation |
| updated_at | timestamptz | Last update |

Unique constraint: (source_id, contract_id)

### vendors
Vendor records from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| source_id | text FK→sources | Which portal |
| vendor_id | text | Bonfire vendor ID |
| company_name | text | Vendor company name |
| first_seen_at | timestamptz | When scraper first found it |
| last_seen_at | timestamptz | Most recent scrape |
| created_at | timestamptz | Row creation |
| updated_at | timestamptz | Last update |

Unique constraint: (source_id, vendor_id)

### scrape_runs
Execution log for each scrape run.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| source_id | text FK→sources | Which portal was scraped |
| started_at | timestamptz | Run start time |
| finished_at | timestamptz | Run end time |
| status | text | "running", "success", "failed" |
| records_found | jsonb | Count of records discovered |
| records_upserted | jsonb | Count of records inserted/updated |
| records_changed | jsonb | Count of records that changed |
| errors | jsonb | Array of error objects (empty [] on success) |
| duration_ms | integer | Run duration in milliseconds |

## Current Sources

| source_id | org_id | portal_host | enabled |
|-----------|--------|-------------|---------|
| hillsborough | 747 | hillsboroughcounty.bonfirehub.com | true |
| pasco | 702 | pascocountyfl.bonfirehub.com | true |

## Allowed URLs

The auditor may request these exact URLs to verify API availability.

| URL | Purpose |
|-----|---------|
| https://hillsboroughcounty.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData | Hillsborough open opportunities |
| https://hillsboroughcounty.bonfirehub.com/PublicPortal/getPastPublicOpportunitiesSectionData | Hillsborough past opportunities |
| https://hillsboroughcounty.bonfirehub.com/PublicPortal/getPublicContractsSectionData | Hillsborough public contracts |
| https://pascocountyfl.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData | Pasco open opportunities |
| https://pascocountyfl.bonfirehub.com/PublicPortal/getPastPublicOpportunitiesSectionData | Pasco past opportunities |
| https://pascocountyfl.bonfirehub.com/PublicPortal/getPublicContractsSectionData | Pasco public contracts |

No other URLs are permitted. Any curl request not matching one of these exact URLs must be rejected by the validator.

## Service Schedule

- Scraper runs daily at 5:00 AM ET via workflow-orchestrate monitor --exec
- Container: compose-bypass-solid-state-feed-6p6e3c-scraper-1
- Docker hostname on dokploy-network: bid-scraper-postgres (DB), bid-scraper-scraper (app)
