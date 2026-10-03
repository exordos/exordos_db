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

import pytest

from exordos_db.common import pg_tuning


@pytest.mark.parametrize("cpu", [1, 2, 4, 16, 128])
@pytest.mark.parametrize("ram", [480, 512, 1024, 4096, 16384, 65536, 1048576])
def test_profile_baselines_and_scaling(cpu, ram):
    settings = pg_tuning.calculate_settings(cpu, ram)
    connections = settings["max_connections"]
    workers = settings["autovacuum_max_workers"]
    work_kib = int(settings["work_mem"].removesuffix("kB"))
    vacuum_mib = int(settings["autovacuum_work_mem"].removesuffix("MB"))
    assert 4096 <= work_kib <= max(4096, ram * 1024 * 15 // 100 // (connections * 6))
    assert 64 <= vacuum_mib <= max(64, ram // 10 // workers)
    assert connections == 500
    assert workers == 5
    assert settings["max_parallel_workers"] == max(8, cpu)
    assert (
        settings["max_parallel_workers_per_gather"] <= settings["max_parallel_workers"]
    )
    assert settings["max_parallel_workers"] <= settings["max_worker_processes"]


def test_small_node_preserves_baselines():
    settings = pg_tuning.calculate_settings(1, 512)
    assert settings["shared_buffers"] == "128MB"
    assert settings["effective_cache_size"] == "4096MB"
    assert settings["work_mem"] == "4096kB"
    assert settings["maintenance_work_mem"] == "64MB"
    assert settings["autovacuum_work_mem"] == "64MB"
    assert settings["max_worker_processes"] == 8
    assert settings["max_parallel_workers"] == 8
    assert settings["hash_mem_multiplier"] == 2
    assert settings["max_connections"] == 500
    assert settings["autovacuum_max_workers"] == 5
    assert settings["max_parallel_workers_per_gather"] == 2
    assert settings["max_parallel_maintenance_workers"] == 2


def test_large_node_caps_per_operation_memory():
    settings = pg_tuning.calculate_settings(128, 1048576)
    assert settings["work_mem"] == "16384kB"
    assert settings["maintenance_work_mem"] == "1024MB"
    assert settings["autovacuum_work_mem"] == "256MB"


@pytest.mark.parametrize("cpu,ram", [(0, 512), (1, 0), (-1, 4096)])
def test_invalid_resources(cpu, ram):
    with pytest.raises(ValueError):
        pg_tuning.calculate_settings(cpu, ram)


def test_detects_total_guest_memory_and_cpu_affinity(monkeypatch):
    monkeypatch.setattr(pg_tuning.os, "sched_getaffinity", lambda pid: {1, 3})
    values = {"SC_PHYS_PAGES": 1048576, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(pg_tuning.os, "sysconf", values.__getitem__)
    assert pg_tuning.node_resources() == (2, 4096)


def test_guest_memory_below_nominal_size_is_supported(monkeypatch):
    monkeypatch.setattr(pg_tuning.os, "sched_getaffinity", lambda pid: {0})
    values = {"SC_PHYS_PAGES": 122880, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(pg_tuning.os, "sysconf", values.__getitem__)
    cpu, ram = pg_tuning.node_resources()
    assert ram == 480
    assert pg_tuning.calculate_settings(cpu, ram)["shared_buffers"] == "128MB"
