#!/bin/bash

set -ue
set -o pipefail

sudo systemctl start exordos-db-pg-agent exordos-universal-agent

sudo systemctl restart exordos-patroni
