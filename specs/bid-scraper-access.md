# Bid Scraper Service Access

## Database

- Host: gov-bid-postgres
- Port: 5432
- Database: govbids
- User: auditor_ro
- Password: auditor_ro_readonly

Connection command:
```
PGPASSWORD=auditor_ro_readonly psql -h gov-bid-postgres -p 5432 -U auditor_ro -d govbids
```

## Tables

### bid_opportunities
Procurement listings scraped from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| project_id | text PK | Bonfire project ID |
| reference_id | text | Bonfire reference ID |
| project_name | text | Listing title |
| status_id | text | Bonfire status code |
| sub_status_id | text | Sub-status |
| department_id | text | Department |
| close_date | timestamptz | Bid deadline |
| source | text | "openOpportunities" or "pastOpportunities" |
| content_hash | text | Hash for change detection |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### public_contracts
Awarded contracts from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| contract_id | text PK | Bonfire contract ID |
| name | text | Contract title |
| vendor_id | text | Vendor reference |
| vendor_name | text | Vendor name |
| department_id | text | Department |
| organization_id | text | Organization |
| contract_status_id | text | Contract status code |
| is_extendable | boolean | Whether contract is extendable |
| start_date | timestamptz | Contract start |
| end_date | timestamptz | Contract end |
| content_hash | text | Hash for change detection |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### vendors
Vendor records from Bonfire portals.

| Column | Type | Notes |
|--------|------|-------|
| vendor_id | text PK | Bonfire vendor ID |
| vendor_name | text | Vendor company name |
| first_seen_at | timestamptz | When scraper first found it |
| last_updated | timestamptz | Last update time |
| raw_data | jsonb | Full API response |

### document_takers
Companies that downloaded bid documents for an opportunity.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| project_id | text FK->bid_opportunities | Which opportunity |
| vendor_name | text | Company that took documents |
| first_seen_at | timestamptz | When first seen |
| last_updated | timestamptz | Last update time |

Unique constraint: (project_id, vendor_name)

### scrape_runs
Execution log for each scrape run.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| started_at | timestamptz | Run start time |
| finished_at | timestamptz | Run end time |
| status | text | "running", "success", "failed" |
| phase | text | Which scrape phase |
| records_processed | integer | Count processed |
| records_new | integer | Count new records |
| records_updated | integer | Count updated records |
| error_message | text | Error details if failed |
| summary | jsonb | Run summary data |

### contract_opportunity_map
Links contracts to opportunities they were awarded from.

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | Auto-increment |
| contract_id | text | Contract reference |
| opportunity_project_id | text FK->bid_opportunities | Opportunity reference |
| match_method | match_method | How the match was determined |
| confidence | real | Match confidence 0.0-1.0 |
| created_at | timestamptz | When mapping was created |

Unique constraint: (contract_id, opportunity_project_id, match_method)

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
- Docker hostname on dokploy-network: gov-bid-postgres (DB), bid-scraper-scraper (app)
