# Container Naming Standards

## Convention

Pattern: `{service}-{component}` -- lowercase, hyphenated, human-readable.

- **`container_name:`** controls what appears in `docker ps`, `docker exec`,
  logs, and ops tooling. Every Dokploy compose MUST set this explicitly.
- **`hostname:`** controls DNS resolution on `dokploy-network`. Used for
  inter-container communication. Only set when needed (databases, services
  that other containers connect to by name).
- These are independent: a container can have `container_name: etl-postgres`
  and `hostname: ds-etl-postgres` (the hostname stays stable for backward
  compatibility with env vars like `DB_HOST`).

## Rules

1. Every Dokploy compose service MUST include a `container_name:` directive.
2. Names use `{service}-{component}` format (e.g., `bid-scraper-postgres`).
3. Single-service stacks drop the component (e.g., `crowdsec`, `open-webui`).
4. Do NOT change `hostname:` values without updating all `DB_HOST` / connection
   string references across the stack.
5. Dokploy swarm services (`dokploy`, `dokploy-postgres`, `dokploy-redis`)
   cannot use `container_name:` -- swarm manages those names.

## Current Inventory

| Container Name | Image | Compose Project | Hostname |
|---|---|---|---|
| `dokploy` | dokploy/dokploy:v0.26.6 | (swarm) | -- |
| `dokploy-postgres` | postgres:16 | (swarm) | -- |
| `dokploy-redis` | redis:7 | (swarm) | -- |
| `dokploy-traefik` | traefik:v3.6.7 | (standalone) | -- |
| `cloudflared-*` | cloudflare/cloudflared | (swarm) | -- |
| `open-webui` | open-webui:v0.6.36 | openwebui-la7vl8 | -- |
| `etl-postgres` | postgres:16-alpine | ds-etl-nhdcjb | `ds-etl-postgres` |
| `etl-scheduler` | defendershield-etl:latest | ds-etl-nhdcjb | -- |
| `etl-jupyter` | defendershield-etl:latest | ds-etl-nhdcjb | -- |
| `bid-scraper-postgres` | postgres:16-alpine | compose-bypass-... | `gov-bid-postgres` |
| `bid-scraper` | gov-bid-scrape:latest | compose-bypass-... | -- |
| `n8n` | n8nio/n8n:latest | compose-hack-virtual-... | -- |
| `n8n-postgres` | postgres:16-alpine | compose-hack-virtual-... | -- |
| `crowdsec` | crowdsecurity/crowdsec | compose-parse-... | `crowdsec-engine` |
| `discord-capture-bot` | discord-capture-bot | compose-program-... | -- |
| `monitoring-cadvisor` | cadvisor:v0.51.0 | compose-copy-... | -- |
| `monitoring-grafana` | grafana:11.5.2 | compose-copy-... | -- |
| `monitoring-node-exporter` | node-exporter:v1.8.2 | compose-copy-... | -- |
| `monitoring-prometheus` | prometheus:v3.2.1 | compose-copy-... | -- |
| `workflow-sentinel` | workflow-sentinel:latest | compose-back-up-... | -- |
| `dozzle` | dozzle:latest | compose-hack-optical-... | -- |
| `homepage` | homepage:latest | compose-navigate-... | -- |
| `obsidian-remote` | obsidian-remote | (manual) | -- |
