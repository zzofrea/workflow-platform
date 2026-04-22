# workflow-platform

Disciplined development lifecycle platform on top of [Dokploy](https://dokploy.com). Two CLIs
glue together dev environment management, AI-powered auditing (via
[workflow-agent](https://github.com/zzofrea/workflow-agent)), deployment gating, and
observability into a repeatable service lifecycle.

## CLIs

### `workflow-env`

Manages isolated dev environments in Dokploy by duplicating the production environment for a
single service.

```
workflow-env up <service>       Duplicate prod env; rewrites compose hostnames to avoid
                                DNS collisions on dokploy-network (e.g. ds-etl-postgres ->
                                ds-etl-dev-postgres). Resource guard checks container count
                                and free RAM before proceeding.
workflow-env down <service>     Stop dev containers (preserves volumes).
workflow-env destroy <service>  Remove dev environment entirely.
workflow-env list               Show all active dev environments.
```

Env vars: `DOKPLOY_API_KEY`, `DOKPLOY_URL` (default `http://localhost:3000`).
Optional resource guard thresholds: `MAX_CONTAINERS` (default 18), `MIN_FREE_RAM_MB` (default 3072).

### `workflow-orchestrate`

Chains workflow-env, workflow-agent, git push, and workflow-notify into a disciplined lifecycle.
Human confirmation gates every irreversible action. Auditing is fully delegated to
`workflow-agent`, which resolves roles, policies, and specs from its own `agents/` directory.

```
workflow-orchestrate build --service <svc>
    Spin up dev env -> run workflow-agent auditor -> push metrics -> print scenario report.
    Exits 1 if audit fails.

workflow-orchestrate deploy --service <svc> --repo <path> [--branch main]
    Verify latest audit is PASS -> human confirm -> git push -> Discord notify -> offer dev teardown.
    Use --skip-audit-check to bypass the audit gate (not recommended).

workflow-orchestrate monitor --service <svc> [--exec "python -m module run"]
    Optionally run a command via docker exec on the service container, then run the auditor
    against live prod. Exec output is archived alongside the audit report.
    Exits 1 if audit fails.

workflow-orchestrate dag <service>
    Load dags/<service>.yaml and execute the DAG. Stages run in topological order; stages at
    the same dependency tier run concurrently. Supports day-of-week (when) and day-of-month
    (when_day_of_month) filters, condition gates (stage.success), and both docker-exec and
    agent stage types. Exits 1 if any stage fails.

workflow-orchestrate briefing <morning|consolidate|weekly>
    Gather context from the daily-briefing-agent container -> Claude synthesis via
    workflow-agent -> post to Discord + write back to Open Brain. Each phase fails fast
    with a workflow-notify warning.
```

## DAG Engine

DAGs are defined in `dags/<service>.yaml`. The engine validates dependencies, detects cycles,
and resolves a topological execution order into parallel tiers.

```yaml
# dags/defendershield-etl.yaml (example)
service: defendershield-etl
schedule: "15 11 * * *"
stages:
  - name: etl-pipeline
    type: docker-exec
    container: etl-scheduler
    command: "python -m defendershield_etl.pipelines.daily_runner --catchup"
    timeout: 3600
  - name: auditor
    type: agent
    role: auditor
    depends_on: [etl-pipeline]
  - name: weekly-report
    type: docker-exec
    container: etl-scheduler
    command: "python -m defendershield_etl.reports.weekly"
    depends_on: [auditor]
    condition: etl-pipeline.success   # skip if ETL failed
    when: [mon]                        # day-of-week filter
```

## Metrics

After every `build`, `monitor`, and `dag` run, metrics are pushed best-effort to Prometheus
Pushgateway (`PUSHGATEWAY_URL`, default `localhost:9091`):

| Metric | Description |
|---|---|
| `agent_run_result` | 1 = pass/complete, 0 = fail/error |
| `agent_run_duration_seconds` | Wall time for the agent run |
| `agent_run_scenarios_pass` | Scenarios that passed |
| `agent_run_scenarios_fail` | Scenarios that failed |

Labels: `service`, `role` (and `stage` for DAG runs). Job name:
`workflow_agent_{service}_{role}[_{stage}]`. Push failures log a warning and do not affect
the run outcome.

Run outputs (reports, exec logs) are archived to `~/agent-output/<service>/<role>_<timestamp>/`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check --fix . && ruff format .
pyright .
pytest
```

Requires Python 3.11+. Depends on
[workflow-notify](https://github.com/zzofrea/workflow-notify) (notifications) and
[workflow-agent](https://github.com/zzofrea/workflow-agent) (audit/synthesis runs).
