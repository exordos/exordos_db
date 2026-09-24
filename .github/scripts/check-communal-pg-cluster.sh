#!/usr/bin/env bash
# Check that the instance communal_pg_cluster ordered through dbaas is up.
#
# Usage: check-communal-pg-cluster.sh [timeout-seconds]
#
# Run once dbaas and communal_pg_cluster are ACTIVE, with the CLI pointed at
# the realm.  An ACTIVE element only means dbaas accepted its resource; dbaas'
# own view of the instance follows the node set that runs postgres, so ACTIVE
# here means the data plane nodes came up as well.
set -uo pipefail

instance="communal-pg-cluster"
timeout="${1:-600}"

deadline=$((SECONDS + timeout))
status=""
while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(exordos dbaas i l -f "name=$instance" -o json 2>/dev/null \
        | jq -r '.[0].status // ""' 2>/dev/null || true)"
    case "$status" in
        ACTIVE)
            echo "PG instance $instance is ACTIVE"
            exordos dbaas i l
            exit 0
            ;;
        ERROR)
            echo "PG instance $instance went ERROR" >&2
            exordos dbaas i l >&2 || true
            exit 1
            ;;
    esac
    echo "Waiting for PG instance $instance... (${status:-not listed})"
    sleep 10
done

echo "PG instance $instance did not become ACTIVE within ${timeout}s" \
     "(last: ${status:-unknown})" >&2
exordos dbaas i l >&2 || true
exit 1
