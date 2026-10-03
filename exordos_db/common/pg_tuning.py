#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Deterministic PostgreSQL settings for dedicated Linux DP virtual machines."""

import os


def node_resources() -> tuple[int, int]:
    """Return CPU affinity size and total guest RAM in MiB."""
    cpu = len(os.sched_getaffinity(0))
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024**2)
    if cpu < 1 or ram < 1:
        raise ValueError("PostgreSQL tuning requires positive CPU and RAM values")
    return cpu, ram


def calculate_settings(cpu: int, ram: int) -> dict[str, int | str]:
    """Size a VM profile; ram is total guest memory in MiB.

    PG18 defaults and fixed connection/vacuum limits override sizing budgets
    on small nodes. These heuristics are not hard memory limits.
    """
    if cpu < 1 or ram < 1:
        raise ValueError("PostgreSQL tuning requires positive CPU and RAM values")
    connections = 500
    vacuum_workers = 5
    parallel = max(2, min(4, cpu // 2))
    work_kib = max(
        4 * 1024, min(16 * 1024, ram * 1024 * 15 // 100 // (connections * 6))
    )
    return {
        "shared_buffers": f"{max(128, ram // (8 if ram < 1024 else 4))}MB",
        "effective_cache_size": f"{max(4096, ram * 7 // 10)}MB",
        "work_mem": f"{work_kib}kB",
        "hash_mem_multiplier": 2,
        "maintenance_work_mem": f"{max(64, min(1024, ram // 20))}MB",
        "autovacuum_work_mem": f"{max(64, min(256, ram // 10 // vacuum_workers))}MB",
        "autovacuum_max_workers": vacuum_workers,
        "max_connections": connections,
        "max_worker_processes": max(8, cpu + 4),
        "max_parallel_workers": max(8, cpu),
        "max_parallel_workers_per_gather": parallel,
        "max_parallel_maintenance_workers": parallel,
    }
