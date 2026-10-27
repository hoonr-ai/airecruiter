#!/usr/bin/env bash
# Postgres server settings for the monitored_jobs lock-timeout investigation
# (Azure Database for PostgreSQL Flexible Server).
#
#   log_lock_waits = on                          log who blocks whom once a lock
#                                                wait passes deadlock_timeout (1s)
#   idle_in_transaction_session_timeout = 60s    end sessions left idle inside
#                                                a transaction (was 0 = never)
#   pg_stat_statements                           per-query timing, to find the
#                                                slowest queries
#
# Idempotent. log_lock_waits and the idle timeout apply without a restart.
# pg_stat_statements needs a server RESTART the first time; this script never
# restarts — it prints the command for a quiet window.
#
# Before turning on the idle timeout, run deployment/sql/check-idle-in-transaction.sql
# against the database: any session it lists would be ended by the new limit.
#
# Usage (Azure CLI, `az login`, rights on the server):
#   RESOURCE_GROUP=<rg> ./deployment/postgres-tuning.sh            # show only
#   RESOURCE_GROUP=<rg> APPLY=1 ./deployment/postgres-tuning.sh    # change
#
# Optional: DB_NAME (default job-diva-db), DB_RESOURCE_GROUP.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?set RESOURCE_GROUP (resource group of the prod VM)}"
DB_NAME="${DB_NAME:-job-diva-db}"
DB_RESOURCE_GROUP="${DB_RESOURCE_GROUP:-$RESOURCE_GROUP}"
APPLY="${APPLY:-0}"

param() {
  az postgres flexible-server parameter show -g "$DB_RESOURCE_GROUP" -s "$DB_NAME" -n "$1" --query value -o tsv
}

set_param() {
  # name wanted-value
  local name=$1 want=$2 have
  have=$(param "$name")
  if [ "$have" = "$want" ]; then
    echo "  $name = $have (already)"
  elif [ "$APPLY" = 1 ]; then
    az postgres flexible-server parameter set -g "$DB_RESOURCE_GROUP" -s "$DB_NAME" -n "$name" --value "$want" -o none
    echo "  $name: $have -> $want"
  else
    echo "  $name: $have -> $want (dry run; APPLY=1 to change)"
  fi
}

# "a,b" plus item, unless already in it (case-insensitive).
with_item() {
  local list=$1 item=$2
  if echo ",$list," | tr '[:upper:]' '[:lower:]' | grep -q ",$(echo "$item" | tr '[:upper:]' '[:lower:]'),"; then
    echo "$list"
  elif [ -z "$list" ]; then
    echo "$item"
  else
    echo "$list,$item"
  fi
}

echo "==> $DB_NAME ($DB_RESOURCE_GROUP)"
set_param log_lock_waits on
set_param idle_in_transaction_session_timeout 60000  # ms

# pg_stat_statements: allow-listed (azure.extensions), preloaded
# (shared_preload_libraries, restart), and created in the app database.
set_param azure.extensions "$(with_item "$(param azure.extensions)" PG_STAT_STATEMENTS)"
preload_before=$(param shared_preload_libraries)
set_param shared_preload_libraries "$(with_item "$preload_before" pg_stat_statements)"
set_param pg_stat_statements.track top

if [ "$(with_item "$preload_before" pg_stat_statements)" != "$preload_before" ]; then
  echo
  echo "pg_stat_statements is loaded only after a restart. In a quiet window:"
  echo "  az postgres flexible-server restart -g $DB_RESOURCE_GROUP -n $DB_NAME"
fi
echo
echo "Then, connected to the app database:"
echo "  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
echo "and review with deployment/sql/top-queries.sql."
