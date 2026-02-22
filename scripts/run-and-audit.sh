#!/bin/bash
# run-and-audit.sh -- Run a service then audit it.
#
# Usage: run-and-audit.sh <service-name>
#
# Executes the service's daily command via docker exec, then fires the
# behavioral auditor container against it. Archives the report and sends
# a Discord notification with the result.
#
# Designed for host crontab. Replaces Dokploy cron for services that
# need post-run auditing.

set -uo pipefail

SERVICE="${1:?Usage: run-and-audit.sh <service-name>}"
SPECS_DIR="/home/docker/workflow-platform/specs"
AUDIT_OUTPUT="/tmp/audit-output-${SERVICE}"
CLAUDE_JSON="/home/docker/.claude.json"
CLAUDE_DIR="/home/docker/.claude"
LOG_TAG="run-and-audit[${SERVICE}]"
DISCORD_WEBHOOK="$(cat /home/docker/monitoring/brain-dump-webhook.txt 2>/dev/null || true)"

log() { echo "[$(date -u +%FT%TZ)] ${LOG_TAG} $*"; }

discord() {
    local msg="$1"
    if [ -n "$DISCORD_WEBHOOK" ]; then
        curl -sf -H "Content-Type: application/json" \
            -d "$(jq -n --arg content "$msg" '{content: $content}')" \
            "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
    fi
}

# -- Service-specific configuration --
case "$SERVICE" in
    bid-scraper)
        CONTAINER="compose-bypass-solid-state-feed-6p6e3c-scraper-1"
        COMMAND="python -m bid_scraper run"
        ;;
    defendershield-etl)
        CONTAINER="ds-etl-nhdcjb-etl-scheduler-1"
        COMMAND="python -m defendershield_etl.pipelines.daily_runner --catchup"
        ;;
    *)
        log "ERROR: Unknown service '$SERVICE'"
        exit 1
        ;;
esac

SPEC="${SPECS_DIR}/${SERVICE}.md"
ACCESS="${SPECS_DIR}/${SERVICE}-access.md"

# Validate spec files exist
if [ ! -f "$SPEC" ] || [ ! -f "$ACCESS" ]; then
    log "ERROR: Missing spec or access doc for $SERVICE"
    exit 1
fi

# -- Phase 1: Run the service --
log "Starting service run..."
SERVICE_OUTPUT=$(docker exec "$CONTAINER" $COMMAND 2>&1)
SERVICE_EXIT=$?
log "Service exited with code $SERVICE_EXIT"

if [ $SERVICE_EXIT -ne 0 ]; then
    log "WARNING: Service run failed. Proceeding to audit anyway."
    discord "**${SERVICE}** service run FAILED (exit $SERVICE_EXIT). Auditor will still verify."
fi

# -- Phase 2: Run the auditor --
log "Starting behavioral audit..."
mkdir -p "$AUDIT_OUTPUT"
rm -f "$AUDIT_OUTPUT/report.json" "$AUDIT_OUTPUT/report.md"

# Remove any stale auditor container with the same name
docker rm -f "auditor-${SERVICE}-prod" 2>/dev/null || true

docker run --rm \
    --name "auditor-${SERVICE}-prod" \
    --network dokploy-network \
    -v "$CLAUDE_JSON:/audit/auth/.claude.json:ro" \
    -v "$CLAUDE_DIR:/audit/auth/.claude:ro" \
    -v "$SPEC:/audit/input/spec.md:ro" \
    -v "$ACCESS:/audit/input/access.md:ro" \
    -v "$AUDIT_OUTPUT:/audit/output:rw" \
    -e AUDITOR_MODE=prod \
    -e AUDITOR_MODEL=sonnet \
    -e "AUDITOR_SERVICE=$SERVICE" \
    -e AUDITOR_MAX_TURNS=25 \
    -e HOME=/home/node \
    workflow-auditor:latest 2>&1

AUDIT_EXIT=$?
log "Auditor exited with code $AUDIT_EXIT"

# -- Phase 3: Archive and notify --
OVERALL="error"
SUMMARY="No report produced"
SCENARIOS_PASS=0
SCENARIOS_TOTAL=0

if [ -f "$AUDIT_OUTPUT/report.json" ]; then
    OVERALL=$(jq -r '.overall // "error"' "$AUDIT_OUTPUT/report.json")
    SUMMARY=$(jq -r '.summary // "No summary"' "$AUDIT_OUTPUT/report.json")
    SCENARIOS_PASS=$(jq -r '.scenarios_pass // 0' "$AUDIT_OUTPUT/report.json")
    SCENARIOS_TOTAL=$(jq -r '.scenarios_total // 0' "$AUDIT_OUTPUT/report.json")

    # Archive
    ARCHIVE_DIR="/home/docker/audit-reports/${SERVICE}/prod_$(date -u +%Y-%m-%d_%H%M%S)"
    mkdir -p "$ARCHIVE_DIR"
    cp "$AUDIT_OUTPUT/report.json" "$ARCHIVE_DIR/"
    cp "$AUDIT_OUTPUT/report.md" "$ARCHIVE_DIR/" 2>/dev/null || true
    log "Report archived to $ARCHIVE_DIR"
fi

# Discord notification
case "$OVERALL" in
    pass)
        discord "**${SERVICE} audit: PASS** (${SCENARIOS_PASS}/${SCENARIOS_TOTAL} scenarios). ${SUMMARY}"
        ;;
    fail)
        discord "**${SERVICE} audit: FAIL** (${SCENARIOS_PASS}/${SCENARIOS_TOTAL} scenarios). ${SUMMARY}"
        ;;
    *)
        discord "**${SERVICE} audit: ${OVERALL^^}** -- ${SUMMARY}"
        ;;
esac

log "Done. Overall: $OVERALL ($SCENARIOS_PASS/$SCENARIOS_TOTAL pass)"
