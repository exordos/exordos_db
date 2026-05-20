#!/bin/bash

set -ue
set -o pipefail

sudo systemctl stop exordos-db-pg-agent exordos-universal-agent
