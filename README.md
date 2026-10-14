# Exordos DBaaS

## Overview

Exordos is a Database as a Service (DBaaS) platform with different DBs support. It provides scalable, managed database services for modern applications.

## Key Features

- 🚀 Auto-scaling and load balancing
- 🔐 Secure access controls and authentication
- 📦 Easy deployment and update with minimal configuration
- 📊 Monitoring and performance insights

## Supported Databases

- ✅ PostgreSQL

## Architecture

The platform consists of:

- Database services (PostgreSQL)
- Management API and Control Plane
- IaaS entities orchestration
- PaaS entities orchestration
- Data Plane image builder

## Monitoring

See [docs/monitoring.md](docs/monitoring.md) for the metrics and logs the nodes
send to the observability element, the Grafana dashboards, and how to query
them, how full the disks are included.

## Audit

Control-plane changes to PostgreSQL versions, instances, users, databases, and
backup policies write SDK audit events in the same transaction as the change.
The service name is `dbaas`. Snapshots exclude passwords, password hashes,
Patroni credentials, and S3 access, secret, and encryption keys, including those
in a restore source. Background changes have no user actor unless an IAM
context is present.

Delivery is disabled by default; local events remain in the outbox. Upgrade
the DBaaS control plane first so its existing SDK migration step creates the
outbox table. To enable delivery, install the separately built
`dbaas_audit` element after DBaaS and Audit. It imports Audit's ingest permission,
creates a dedicated service account and automatically renewed Core managed
token, and writes `/etc/exordos_db/exordos_db.d/audit.conf` with `enabled = true`.
The config belongs to Core's service project. Changes restart the user API,
whose sender drains the shared API/background-service outbox. The main
`dbaas` element does not require Audit.

Control-plane services load additional configuration through
`--config-dir /etc/exordos_db/exordos_db.d`. The central Ecosystem Audit page
can display these events with its service filter set to `dbaas`.
