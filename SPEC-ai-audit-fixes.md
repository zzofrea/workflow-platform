# SPEC: AI Audit Follow-up Fixes

**Date:** 2026-03-22

---

## System Overview

Four targeted fixes identified during a homelab AI deployment audit. The changes harden
three existing systems: the workflow-agent DAG execution engine (loses real metrics data),
the Grafana alerting stack (completely broken — contact points are disabled), and the host
cron + shell environment (sensitive tokens scattered in plaintext across `.bashrc` and
crontab). Additionally, a scheduling conflict between two concurrent 11:15 UTC cron jobs
is resolved by staggering the morning briefing by 15 minutes.

No new systems are created. All changes are narrowly scoped to the identified failures.

---

## Behavioral Contract

### Cron Scheduling

- When the morning briefing cron runs, it fires at 11:30 UTC (not 11:15 UTC).
- When both the ETL DAG (11:15 UTC) and morning briefing (11:30 UTC) run on the same day,
  neither is blocked or delayed by the other — they are separated by 15 minutes.
- When the open-brain auditor (11:00 UTC Sunday) and morning briefing (11:30 UTC Sunday)
  and ETL DAG (11:15 UTC Sunday) all run on the same Sunday, all three start at their
  scheduled times without contention.

### DAG Metrics

- When a workflow-agent DAG run completes an `agent`-type stage, the Agent Observability
  dashboard in Grafana shows the actual pass/fail scenario counts reported by the agent,
  not zero.
- When an agent stage reports `scenarios_pass: 7, scenarios_fail: 1`, Pushgateway
  receives those exact values.
- When an agent stage's report contains no `scenarios_pass` or `scenarios_fail` keys
  (e.g., the open-brain narrative auditor), the metrics push succeeds without error and
  scenario counts default to zero.
- When an agent stage raises an exception (agent container crash, timeout), scenario
  counts default to zero and the result is recorded as ERROR — no metrics push failure.

### Grafana Alerting

- When Grafana starts (or restarts), both `discord-brain-dump` and `discord-general`
  contact points are visible in the Alerting → Contact Points UI as provisioned.
- When Grafana starts, an alert rule named "Agent run failed" exists in the Alerts folder,
  evaluating `agent_run_result{service!~"test-svc"} == 0`.
- When an agent run fails (result metric = 0) and holds that state for 5 minutes, Grafana
  routes an alert to `discord-brain-dump`.
- When a previously failed agent run subsequently succeeds (result metric = 1), Grafana
  sends a resolve notification to `discord-brain-dump`.
- When the open-brain or ETL containers go down for more than 2 minutes, Grafana routes
  an alert to `discord-general` (existing behavior — must be preserved).

### Secrets Consolidation

- When any scheduled cron job runs, it has access to all required environment variables
  (CLAUDE_CODE_OAUTH_TOKEN, Discord webhook URLs, DB passwords, SMTP password, etc.)
  without those values appearing as plaintext in the crontab file.
- When `/home/docker/.agent-secrets` does not exist and a cron job fires, the job fails
  immediately and produces stderr output rather than running with missing credentials.
- When a user opens an interactive shell, all environment variables previously exported
  in `.bashrc` lines 7–18 are still available — behavior is identical to before.
- When `/home/docker/.agent-secrets` is inspected with `ls -la`, permissions are
  `-rw-------` (mode 600, owned by the `docker` user).
- When `.bashrc` is read, no raw token or password values appear in the file.

---

## Explicit Non-Behaviors

- Must not change the schedule of any cron job other than `briefing morning`.
- Must not modify the content or webhook URLs in `contact-points.yaml` — only the filename changes.
- Must not add fields or logic to `_execute_agent()` beyond changing its return type.
- Must not move Dokploy-managed container env vars into `.agent-secrets`.
- Must not use `BASH_ENV` for the cron guard — per-command source required for loud failure.
- Must not add `DISCORD_BRIEFING_WEBHOOK_URL` to `.agent-secrets` (Dokploy-managed).
- Must not introduce a wrapper script for sourcing secrets.
- Must not remove `PYTHONPATH=...` per-command prefixes from crontab entries.

---

## Integration Boundaries

### Pushgateway (`localhost:9091`)

- `scenarios_pass` and `scenarios_fail` now reflect actual agent report values instead of
  always being zero for DAG-mode runs. Best-effort push with try/except — unchanged.

### Grafana provisioning (`/home/docker/monitoring-config/alerting/`)

- `contact-points.yaml` must exist (not `.disabled`) for Grafana to load Discord receivers.
- Changes take effect after `docker restart monitoring-grafana`.
- Prometheus datasource UID: `PBFA97CFB590B2093` — trust existing value (used by all rules).

### `/home/docker/.agent-secrets`

- Plain shell file, sourced via `.` builtin. All lines: `export VAR=value`. No shebang.
- Permissions: `chmod 600`, `chown docker:docker`.
- Contents: `DISCORD_WEBHOOK_URL`, `DISCORD_AGENT_LOGS_WEBHOOK_URL`,
  `DISCORD_BRAIN_DUMP_WEBHOOK_URL`, `BID_SCRAPER_DB_PASSWORD`,
  `OPEN_BRAIN_AUDITOR_DB_PASSWORD`, `AGENT_SMTP_EMAIL`, `AGENT_SMTP_PASSWORD`,
  `AGENT_RECIPIENT_LIST`, `DOKPLOY_API_KEY`, `HCSS_DB_UID`, `HCSS_DB_PWD`,
  `CLAUDE_CODE_OAUTH_TOKEN`.
- Vars with special chars must use single-quote wrapping (e.g., `HCSS_DB_PWD`).

---

## Behavioral Scenarios

### Happy Path

**Scenario 1 — DAG scenario counts reach Grafana**

```
GIVEN the ETL DAG runs its auditor stage
AND the agent report contains scenarios_pass=12 and scenarios_fail=0
WHEN the stage completes
THEN Pushgateway holds agent_run_scenarios_pass{service="defendershield-etl",role="auditor"} = 12
AND Pushgateway holds agent_run_scenarios_fail{service="defendershield-etl",role="auditor"} = 0
AND the Agent Observability dashboard shows "12 passed, 0 failed" for the defendershield-etl auditor
```

**Scenario 2 — Cron jobs run without secrets in plaintext**

```
GIVEN .agent-secrets exists at /home/docker/.agent-secrets with mode 600
AND crontab contains no raw token or password values
WHEN the ETL DAG cron fires at 11:15 UTC
THEN the workflow-orchestrate process starts successfully
AND CLAUDE_CODE_OAUTH_TOKEN is available to the process (agent can authenticate)
AND the crontab file, if read by any process, contains no raw credential values
```

**Scenario 3 — Agent failure alert reaches Discord**

```
GIVEN Grafana has loaded the contact-points.yaml provisioning file
AND an agent run result metric reads 0 (failure) for service "defendershield-etl"
WHEN the metric holds that value for 5 continuous minutes
THEN an alert message appears in Discord #brain-dump
AND the alert message identifies the failing service and role
```

**Scenario 4 — Morning briefing no longer conflicts with ETL DAG**

```
GIVEN the ETL DAG is scheduled at 15 11 * * *
AND the morning briefing is scheduled at 30 11 * * *
WHEN both run on the same day
THEN workflow-monitor.log shows ETL DAG start at ~11:15 UTC
AND briefing.log shows briefing start at ~11:30 UTC
```

### Error Scenarios

**Scenario 5 — Missing .agent-secrets causes loud cron failure**

```
GIVEN .agent-secrets does not exist at /home/docker/.agent-secrets
WHEN any cron job fires
THEN the cron job exits with a non-zero status immediately
AND stderr output is produced indicating the missing file
AND the downstream command does NOT run
```

**Scenario 6 — Agent run failure alert resolves after success**

```
GIVEN an alert "Agent run failed" is firing for service "open-brain" role "auditor"
WHEN the next open-brain auditor run completes successfully (result metric = 1)
THEN Grafana sends a resolve notification to Discord #brain-dump within 1 evaluation cycle
AND the alert state returns to Normal in the Grafana Alerting UI
```

### Edge Cases

**Scenario 7 — Agent with no scenarios does not break metrics**

```
GIVEN the open-brain auditor runs (narrative report, no pass/fail scenarios)
AND its report contains no scenarios_pass or scenarios_fail keys
WHEN the DAG stage completes
THEN Pushgateway receives agent_run_result=1 (complete = success)
AND agent_run_scenarios_pass and agent_run_scenarios_fail default to 0
AND no error is logged for metrics push
```

**Scenario 8 — Sunday three-way scheduling no conflict**

```
GIVEN it is a Sunday
WHEN 11:00 UTC arrives: open-brain auditor fires
AND 11:15 UTC arrives: ETL DAG fires
AND 11:30 UTC arrives: morning briefing fires
THEN all three jobs start at their scheduled time without contention
```

---

## Definition of Done

- [ ] `pytest tests/` passes — all existing tests green, new `_push_stage_metrics` real-path test green
- [ ] `ruff check --fix .` and `ruff format .` produce no changes
- [ ] `pyright .` reports no type errors
- [ ] `crontab -l` shows: no raw credentials, `SHELL=/bin/bash`, morning briefing at `30 11 * * *`
- [ ] `ls -la /home/docker/.agent-secrets` shows `-rw-------`
- [ ] `grep -E 'sk-ant|xlsr|kx1151|aud_ro' /home/docker/.bashrc` returns no matches
- [ ] Grafana UI shows `discord-brain-dump` and `discord-general` as provisioned contact points
- [ ] Grafana UI shows "Agent run failed" rule in Alerts folder
- [ ] Committed on `fix/ai-audit-follow-up` branch with conventional commit messages

---

## Ambiguity Warnings

1. **Crontab edit:** Use `crontab -` via stdin to avoid locking issues.
2. **Grafana notification-policies.yaml:** Adding `routes:` changes YAML structure. If
   Grafana logs provisioning errors after restart, revert to flat structure and add
   agent-run-failed as a sibling matcher.
3. **`push_metrics` defensive access — RESOLVED:** Lines 75-77 of `metrics.py` use `.get()`.
   Passing real report as-is is safe even when scenario keys are absent.
4. **`.bashrc` position:** Replacement must be before line 21 (`case $- in`).
5. **`HCSS_DB_PWD` quoting:** Must use single quotes: `export HCSS_DB_PWD='OOr1Ih5|E_t8$?'`
6. **New test patch target:** Patch `workflow_platform.metrics.push_to_gateway`.
7. **Grafana datasource UID:** After restart, check logs for provisioning errors.

---

## Pre-Implementation Risk Review

**HIGH — Secrets file quoting.** Verify after creation:
```bash
bash -c '. /home/docker/.agent-secrets && echo "HCSS=$HCSS_DB_PWD CLAUDE=${CLAUDE_CODE_OAUTH_TOKEN:0:10}"'
```

**HIGH — Ordering.** Create and verify `.agent-secrets` BEFORE touching crontab.

**MEDIUM — All 5 cron jobs get prefix.** Verify:
```bash
crontab -l | grep -c 'agent-secrets'  # must be 5
crontab -l | grep 'sk-ant\|OAUTH\|webhook\|SMTP'  # must be empty
```

**MEDIUM — Grafana UID and policy YAML.** Check logs after restart:
```bash
docker logs monitoring-grafana 2>&1 | grep -i 'provisioning\|error' | tail -20
```

**LOW — DAG metrics fix.** Purely additive; worst case is today's behavior. No regression.

**NOT A RISK — .bashrc change breaking cron.** Cron uses per-command source, not `.bashrc`.

---

## Implementation Constraints

- **Repo:** `workflow-platform` (`/home/docker/workflow-platform`)
- **Python:** 3.12, type hints required, pyright must pass
- **Linting:** ruff (check + format) before any commit
- **Grafana config:** `/home/docker/monitoring-config/alerting/` — volume-mounted into container
- **Crontab:** Host-level, `docker` user — edit via `crontab` command, not direct file write
- **Secrets file:** `/home/docker/.agent-secrets` — not inside any repo, not git-tracked

---

## Constitution Compliance

| Rule | How satisfied |
|------|--------------|
| 1-6 | Given/When/Then scenarios describe observable outcomes only |
| 7 | Human approval obtained via ExitPlanMode before implementation |
| 8-10 | Two test streams: acceptance scenarios + unit tests (new + existing). pytest runner. |
| 17-19 | Secrets moved to restricted file; no secrets in code/images; no new injection vectors |
| 23 | PROJECT.md already exists for workflow-platform; out-of-scope additions tracked here |
| 24-27 | DoD includes test pass, Grafana restart/verify, monitoring now functional |
| 28-31 | No unrequested features; no wrapper script; minimal dag.py change; delete not comment |
