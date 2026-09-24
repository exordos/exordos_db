#!/usr/bin/env bash
# Overbook the realm's hypervisor so communal_pg_cluster's PG node fits.
#
# Usage: overbook-hypervisor.sh [cores-ratio] [ram-ratio]
#
# Run with the CLI pointed at the realm, before the elements are installed.  A
# warm realm is sized for the pool's defaults, which dbaas plus a PG node with
# the realm's default cores and RAM may not fit one to one; the runner test
# overbooked its local hypervisor the same way.
set -euo pipefail

cores_ratio="${1:-10.0}"
ram_ratio="${2:-10.0}"

hypervisors="$(exordos c h l -o json)"
uuid="$(echo "$hypervisors" | jq -r '.[0].uuid // ""')"
if [ -z "$uuid" ]; then
    echo "The realm reports no hypervisor to overbook" >&2
    exit 1
fi

exordos compute hypervisors update "$uuid" \
    --cores-ratio "$cores_ratio" --ram-ratio "$ram_ratio"
exordos c h l
