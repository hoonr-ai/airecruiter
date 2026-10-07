#!/usr/bin/env bash
# Azure Monitor alerts for Pair production (W10).
#
# Idempotent: re-running recreates each metric alert with the current
# thresholds. Needs the Azure CLI,
# logged in (`az login`) with Monitoring Contributor on the resource group.
#
# Usage:
#   ALERT_EMAILS="oncall@example.com,lead@example.com" \
#   REDIS_NAME=<prod redis cache name> \
#   ./deployment/azure-monitor-alerts.sh
#
# Optional overrides: RESOURCE_GROUP, VM_NAME, DB_NAME, REDIS_RESOURCE_GROUP,
# DB_RESOURCE_GROUP, HEALTH_URL, APP_INSIGHTS_NAME (enables the availability
# test), LOCATION.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?set RESOURCE_GROUP (resource group of the prod VM)}"
VM_NAME="${VM_NAME:-job-diva-machine-prod}"
DB_NAME="${DB_NAME:-job-diva-db}"
DB_RESOURCE_GROUP="${DB_RESOURCE_GROUP:-$RESOURCE_GROUP}"
REDIS_NAME="${REDIS_NAME:-}"
REDIS_RESOURCE_GROUP="${REDIS_RESOURCE_GROUP:-$RESOURCE_GROUP}"
ALERT_EMAILS="${ALERT_EMAILS:?set ALERT_EMAILS (comma-separated)}"
HEALTH_URL="${HEALTH_URL:-https://pair.pyramidci.com/api/health}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-}"
LOCATION="${LOCATION:-$(az group show -n "$RESOURCE_GROUP" --query location -o tsv)}"
ACTION_GROUP="pair-prod-alerts"

echo "==> Action group $ACTION_GROUP"
receivers=()
i=0
IFS=',' read -ra emails <<< "$ALERT_EMAILS"
for e in "${emails[@]}"; do
  i=$((i + 1))
  receivers+=(--action email "oncall$i" "$(echo "$e" | xargs)")
done
az monitor action-group create -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" \
  --short-name pairprod "${receivers[@]}" -o none
AG_ID=$(az monitor action-group show -g "$RESOURCE_GROUP" -n "$ACTION_GROUP" --query id -o tsv)

metric_alert() {
  # name scope condition window severity description
  local name=$1 scope=$2 condition=$3 window=$4 severity=$5 description=$6
  echo "==> $name"
  # `metrics alert update` can't replace a condition (only add/remove by
  # generated name), so changed thresholds would never apply. Recreate instead.
  if az monitor metrics alert show -g "$RESOURCE_GROUP" -n "$name" -o none 2>/dev/null; then
    az monitor metrics alert delete -g "$RESOURCE_GROUP" -n "$name" -o none
  fi
  az monitor metrics alert create -g "$RESOURCE_GROUP" -n "$name" --scopes "$scope" \
    --condition "$condition" --window-size "$window" --evaluation-frequency 5m \
    --severity "$severity" --action "$AG_ID" --description "$description" -o none
}

VM_ID=$(az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" --query id -o tsv)
metric_alert pair-vm-cpu-high "$VM_ID" \
  "avg Percentage CPU > 70" 15m 2 \
  "Prod VM CPU above 70% for 15 min (normal is 3-14%)."
metric_alert pair-vm-memory-low "$VM_ID" \
  "avg Available Memory Bytes < 3221225472" 10m 1 \
  "Prod VM available memory under 3 GB: worker OOM kills are likely."

DB_ID=$(az postgres flexible-server show -g "$DB_RESOURCE_GROUP" -n "$DB_NAME" --query id -o tsv)
metric_alert pair-db-connections-high "$DB_ID" \
  "max active_connections > 150" 5m 2 \
  "Postgres active connections above 150 (max_connections is 200)."
metric_alert pair-db-cpu-high "$DB_ID" \
  "avg cpu_percent > 80" 15m 2 \
  "Postgres CPU above 80% for 15 min."

if [ -n "$REDIS_NAME" ]; then
  REDIS_ID=$(az redis show -g "$REDIS_RESOURCE_GROUP" -n "$REDIS_NAME" --query id -o tsv)
  metric_alert pair-redis-memory-high "$REDIS_ID" \
    "max usedmemorypercentage > 80" 15m 2 \
    "Prod Redis above 80% memory: rate limits and caches fail when it fills."
  metric_alert pair-redis-errors "$REDIS_ID" \
    "total errors > 0" 5m 2 \
    "Prod Redis is returning errors (OOM, rejected connections)."
else
  echo "==> Skipping Redis alerts (set REDIS_NAME once the dedicated prod Redis exists)"
fi

if [ -n "$APP_INSIGHTS_NAME" ]; then
  echo "==> Availability test on $HEALTH_URL"
  AI_ID=$(az monitor app-insights component show -g "$RESOURCE_GROUP" -a "$APP_INSIGHTS_NAME" --query id -o tsv)
  az monitor app-insights web-test create -g "$RESOURCE_GROUP" -n pair-api-health \
    --location "$LOCATION" --kind standard --web-test-kind standard \
    --defined-web-test-name pair-api-health --enabled true --frequency 300 --timeout 30 \
    --retry-enabled true --request-url "$HEALTH_URL" --http-verb GET \
    --expected-status-code 200 \
    --locations Id="us-va-ash-azr" --locations Id="emea-gb-db3-azr" --locations Id="apac-sg-sin-azr" \
    --tags "hidden-link:$AI_ID=Resource" -o none
  # The only availability test on this component, so the component metric is this test.
  metric_alert pair-api-availability "$AI_ID" \
    "avg availabilityResults/availabilityPercentage < 100" 5m 1 \
    "Pair /api/health failing from at least one region."
else
  echo "==> Skipping availability test (set APP_INSIGHTS_NAME to enable)"
fi

echo "Done. Alerts notify action group $ACTION_GROUP."
