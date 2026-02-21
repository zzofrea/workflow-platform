## Objective
A disciplined development lifecycle platform on top of Dokploy: spec -> build in dev -> independent behavioral audit -> human approval -> deploy to prod -> continuous monitoring.

## Acceptance Criteria
- [ ] Unified notification library delivers to Discord, Email, and Obsidian vault based on severity
- [ ] Catchup pattern standardized; bid scraper has gap detection
- [ ] Dev/prod environments isolated in Dokploy, spinnable up/down on demand
- [ ] Behavioral auditor container validates specs against running services without source code access
- [ ] Full lifecycle orchestration connects all phases with human gates

## Constraints
- Security: Secrets in Dokploy env vars only, never in git. Auditor gets read-only DB access.
- Performance: Beelink Ser3 Mini with limited resources; resource guards on dev environments.
- Dependencies: Dokploy v0.26.6, Cloudflare tunnel, GitHub, Discord webhooks, Gmail SMTP.

## What "Done" Means
- [ ] All 5 phases implemented with acceptance tests passing
- [ ] No disruption to existing prod services
- [ ] End-to-end workflow validated on a real project (bid scraper feature)
- [ ] Monitoring active for all services

## Out of Scope
- Multi-user access control (solo developer platform)
- Custom workflow engine (thin CLI glue, not an orchestration framework)
- Automated rollback (fixes go through full dev->validate->approve->deploy cycle)
