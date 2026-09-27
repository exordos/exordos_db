# vmagent scrape configuration template of a DBaaS node, in place of the one
# of the base image. vmagent renders it at start with the node host name in
# place of __HOSTNAME__.
#
# The agent writes the file_sd files: the node one has a single target
# labelled with the instance, its project and the engine, which every job
# of the node scrapes at its own address; the databases one has a target per
# database, each with its DSN in __param_target and the same labels, all
# probed through the per-database exporter.
scrape_configs:
  - job_name: "node_exporter"
    scrape_interval: 15s
    file_sd_configs:
      - files: ["/var/lib/exordos/exordos_db/vmagent_node.json"]
    relabel_configs:
      - target_label: instance
        replacement: "__HOSTNAME__"
      - target_label: __address__
        replacement: "127.0.0.1:9100"
  - job_name: "patroni"
    scrape_interval: 15s
    file_sd_configs:
      - files: ["/var/lib/exordos/exordos_db/vmagent_node.json"]
    relabel_configs:
      - target_label: instance
        replacement: "__HOSTNAME__"
      - target_label: __address__
        replacement: "127.0.0.1:8008"
  - job_name: "postgres_exporter"
    scrape_interval: 15s
    file_sd_configs:
      - files: ["/var/lib/exordos/exordos_db/vmagent_node.json"]
    relabel_configs:
      - target_label: instance
        replacement: "__HOSTNAME__"
      - target_label: __address__
        replacement: "127.0.0.1:9187"
  # About 30 series a table: the limit, per database, keeps a schema of
  # thousands of tables off the shared VictoriaMetrics. The progress of the
  # vacuums of the whole instance comes with every database, with table
  # names only in the one of the probe: that's the one kept.
  - job_name: "postgres_exporter_databases"
    scrape_interval: 60s
    series_limit: 30000
    metrics_path: /probe
    file_sd_configs:
      - files: ["/var/lib/exordos/exordos_db/vmagent_databases.json"]
    relabel_configs:
      - target_label: instance
        replacement: "__HOSTNAME__"
      - target_label: __address__
        replacement: "127.0.0.1:9188"
    metric_relabel_configs:
      - if: '{__name__=~"pg_stat_progress_vacuum_.+"}'
        action: keep_if_equal
        source_labels: [datname, database]
